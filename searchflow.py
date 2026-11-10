"""Общий поток поиска: план -> уточняющие вопросы -> активный поиск.
Используется и ботом (aiogram), и мини-app сайтом.
"""
import asyncio
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import appconfig
import ai as ai_client
import db
from categories import CITIES

log = logging.getLogger("flow")

CITY_DEFAULT_MARK = "(по умолчанию)"


def city_label(slug: str) -> str:
    for name, s in CITIES.items():
        if s == slug:
            return name.capitalize()
    return slug


def cities_options(plan: dict) -> list:
    default_cities = appconfig.cfg("default_city_slugs", ["novokuznetsk"])
    plan_cities = list(plan.get("city_slugs") or [])
    same = sorted(map(str, plan_cities)) == sorted(map(str, default_cities))
    opts = []
    if not same:
        opts.append(f"{', '.join(city_label(c) for c in plan_cities)} (как в запросе)")
    opts.append(f"{', '.join(city_label(c) for c in default_cities)} {CITY_DEFAULT_MARK}")
    opts.append("Вся Россия + Авито Доставка")
    opts.append("✍️ Другие города — напишу текстом")
    return opts


def build_questions(plan: dict) -> list:
    return [{"id": "cities", "question": "В каких городах ищем?", "options": cities_options(plan)}] \
        + list(plan.get("clarify") or [])


def has_running_dialog(chat_id: int) -> bool:
    """Нельзя вести два поиска одновременно: есть PLANNING/ASKING у юзера?"""
    conn = db._conn()
    row = conn.execute(
        "SELECT 1 FROM searches WHERE chat_id = ? AND status IN ('PLANNING', 'ASKING') LIMIT 1",
        (chat_id,),
    ).fetchone()
    conn.close()
    return row is not None


async def plan_search(sid: int, raw_text: str, default_cities: list) -> bool:
    """Нейросеть собирает план для уже созданной записи (status=PLANNING).
    Успех -> ASKING (+questions), неудача -> PLANNING_FAILED."""
    s = db.get_search(sid)
    if not s:
        return False
    plan = await ai_client.plan_request(raw_text, default_cities)
    if not plan:
        # автоповтор ещё раз
        await asyncio.sleep(15)
        plan = await ai_client.plan_request(raw_text, default_cities)
    if not plan:
        db.set_search_status(sid, "PLANNING_FAILED")
        return False
    questions_list = build_questions(plan)          # берём clarify из плана
    plan.pop("clarify", None)
    plan.pop("clarify_questions", None)
    # записываем профиль и вопросы (города — первый вопрос)
    conn = db._conn()
    conn.execute(
        "UPDATE searches SET title = ?, profile = ?, questions = ?, status = 'ASKING' WHERE id = ?",
        (plan.get("title", "Поиск"), json.dumps(plan, ensure_ascii=False),
         json.dumps(questions_list, ensure_ascii=False), sid),
    )
    conn.commit()
    conn.close()
    _wake()
    return True


def record_answer(sid: int, idx: int, answer: str) -> dict | None:
    """Сохранить ответ на вопрос idx (+ доставка при не-дефолтных городах).
    Возвращает словарь вопроса, который надо задать следующим, либо None если вопросы закончились."""
    s = db.get_search(sid)
    if not s or idx >= len(s["questions"]) or s["status"] not in ("ASKING",):
        return None
    q = s["questions"][idx]
    db.append_answer(sid, {"question": q.get("question", ""), "answer": answer})

    questions = list(s["questions"])
    if q.get("id") == "cities" and CITY_DEFAULT_MARK not in answer and "Доставка" not in answer:
        if not any(qq.get("id") == "delivery" for qq in questions):
            questions.insert(idx + 1, {
                "id": "delivery",
                "question": "Авито Доставка нужна?",
                "options": ["Только с Авито Доставкой", "Только обычные объявления", "Без разницы"],
            })
            db.update_questions(sid, questions)
            return questions[idx + 1]

    if idx + 1 < len(questions):
        return questions[idx + 1]
    return None


def next_question_index(sid: int) -> int:
    s = db.get_search(sid)
    return len(s["answers"]) if s else 0


async def finalize_search(sid: int) -> dict | None:
    """Влить ответы в профиль и активировать поиск."""
    s = db.get_search(sid)
    if not s:
        return None
    profile, answers = s["profile"], s["answers"]
    if answers:
        merged = await ai_client.merge_answers(profile, answers)
        if not merged:
            merged = profile
        profile = merged
    profile.pop("clarify", None)
    db.update_search_profile(sid, profile, answers)
    db.set_search_status(sid, "ACTIVE")
    _wake()
    return db.get_search(sid)


def _wake():
    try:
        import bot
        bot._wake.set()
    except Exception:
        pass
