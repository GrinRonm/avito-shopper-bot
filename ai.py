"""Работа с нейросетью (BeatAPI): все вызовы через общий кулдаун.

Функции:
- plan_request       — разбор свободного текста юзера в «профиль поиска» + уточняющие вопросы
- merge_answers      — вливание ответов юзера в профиль
- parse_feedback     — разбор «не подходит, потому что …» (реплай на уведомление)
- screen_items       — быстрый отсеев по заголовкам/сниппетам (групповой, 1 вызов на цикл)
- verify_items       — глубокая сверка карточек с требованиями (групповой, 1 вызов на цикл)
"""
import asyncio
import json
import logging
import re
import time
import urllib.error
import urllib.request

import appconfig
from categories import categories_prompt_line, cities_prompt_line

log = logging.getLogger("ai")

BEATAPI_URL = appconfig.cfg("beatapi_url")
BEATAPI_KEY = appconfig.cfg("beatapi_key")
BEATAPI_MODEL = appconfig.cfg("beatapi_model", "deepseek-v4.1-flash-free")

_cool_lock = asyncio.Lock()
_last_call = 0.0

# --------------------------------------------------------------- json helpers

def _try_json_from(text: str, opener: str, closer: str):
    """Ищет валидный JSON начиная с первой opener-скобки, перебирая
    позиции закрывающих с конца (чтобы отрезать болтовню модели после массива)."""
    start = text.find(opener)
    if start == -1:
        return None
    tail = text[start:]
    ends = [m.start() for m in re.finditer(re.escape(closer), tail)]
    for e in reversed(ends):
        try:
            return json.loads(tail[:e + 1])
        except Exception:
            continue
    return None


def _extract_json(text: str):
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    # порядок пробуем по тому, что в тексте встречается раньше:
    # это не даёт внутреннему массиву (city_slugs и т.п.) перехватить разбор объекта
    ip, ib = text.find("["), text.find("{")
    if ip == -1:
        order = (("{", "}"), ("[", "]"))
    elif ib == -1:
        order = (("[", "]"), ("{", "}"))
    else:
        order = (("[", "]"), ("{", "}")) if ip < ib else (("{", "}"), ("[", "]"))
    for opener, closer in order:
        res = _try_json_from(text, opener, closer)
        if res is not None:
            return res
    return None


def _grab_str(chunk: str, key: str) -> str:
    """Достаёт строковое поле из «сломанного» JSON: до ближайшего `", "` либо до конца."""
    m = re.search(rf'"{key}"\s*:\s*"(.*?)"(?:\s*(?:,|\}}|\]|$))', chunk, re.DOTALL)
    if m:
        return m.group(1)
    return ""


def _lenint_records(text: str) -> list:
    """Резервный парсер записей по регэкспам, когда JSON от модели битый."""
    out = []
    chunks = re.split(r'\{\s*"item_id"', text)
    for chunk in chunks[1:]:
        chunk = '"item_id"' + chunk
        rec = {"item_id": _grab_str(chunk, "item_id")}
        if not rec["item_id"]:
            continue
        m = re.search(r'"(?:search_id|sudba_id)"\s*:\s*(\d+)', chunk)
        rec["search_id"] = int(m.group(1)) if m else None
        verdict = re.search(r'"(?:verdict|screen)"\s*:\s*"([A-Za-zА-Яа-я]+)"', chunk)
        rec["screen"] = rec["verdict"] = verdict.group(1).upper() if verdict else ""
        for f in ("model", "reason", "why", "tips"):
            rec[f] = _grab_str(chunk, f)
        out.append(rec)
    return out


# --------------------------------------------------------------- beatapi core

async def _beatapi(prompt: str, tag: str) -> str:
    """Один запрос к нейросети с общим кулдауном и ретраем на 429."""
    global _last_call
    async with _cool_lock:
        wait = 64 - (time.time() - _last_call)
        if wait > 0:
            log.info(f"⏳ Кулдаун нейросети: {int(wait)} сек ({tag})")
            await asyncio.sleep(wait)

        payload = {
            "model": BEATAPI_MODEL,
            "input": [{"role": "user", "content": prompt}],
        }
        headers = {
            "Authorization": f"Bearer {BEATAPI_KEY}",
            "Content-Type": "application/json",
        }

        def _do() -> bytes:
            req = urllib.request.Request(BEATAPI_URL, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=headers)
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.read()

        for attempt in range(3):
            try:
                raw = await asyncio.to_thread(_do)
                _last_call = time.time()
                data = json.loads(raw.decode("utf-8"))
                for item in data.get("output", []):
                    if item.get("type") == "message":
                        for block in item.get("content", []):
                            if block.get("type") == "output_text":
                                return block.get("text", "")
                return ""
            except urllib.error.HTTPError as he:
                _last_call = time.time()
                if he.code == 429:
                    log.info("⚠️ 429 от BeatAPI, жду 65 сек...")
                    await asyncio.sleep(65)
                    continue
                raise
            except Exception as e:
                if attempt < 2:
                    await asyncio.sleep(5)
                    continue
                raise
    return ""


# --------------------------------------------------------------- профиль поиска

PROFILE_SYSTEM = """Ты — AI-помощник покупателя для Авито. Твоя задача — превратить свободный текст человека в структурированный ПРОФИЛЬ ПОИСКА.

Доступные категории Авито (slug):
""" + categories_prompt_line() + """

Известные города (slug):
""" + cities_prompt_line() + """

Города, которых нет в списке, транслитерируй латиницей маленькими буквами (Красноярск -> krasnoyarsk). Если человек ищет по всей России — ["rossiya"].

Просим структурированный JSON без пояснений:
{
  "title": "короткое название поиска (например: 'Телефон на Android 128ГБ+')",
  "category_slug": "slug из списка или свой (если совсем нет подходящего — null)",
  "search_queries": ["фраза для URL Авито ?q=...", "фраза 2"],
  "title_query": "основная поисковая фраза (первая из search_queries, для совместимости)",
  "city_slugs": ["slug", ...],
  "price_min": число или null,
  "price_max": число или null,
  "must": [...],
  "must_not": [...],
  "nice": [...],
  "clarify": []
}

Правила для search_queries:
- Если человек перечисляет несколько брендов/моделей/вариантов — сделай по одной фразе на каждый вариант, добавив ключевую характеристику.
- Например: "Honor / Redmi / Xiaomi, ноутбук Ryzen 16/512" -> ["honor ноутбук ryzen 16", "redmi ноутбук ryzen 16", "xiaomi ноутбук ryzen 16"]. Максимум 5 фраз.
- Если вариант один — можно 1-2 фразы. Не дублируй почти одинаковые фразы. Не используй кавычки внутри фраз.

Правила для clarify:
- Задавай вопросы ТОЛЬКО если действительно не хватает важной информации (бюджет, ключевые характеристики).
- Максимум 3 вопроса, каждый с 2-4 вариантами ответа + последний вариант "Не важно".
- Если всё уже понятно — верни пустой список [].
- В must/must_not пиши требования понятными любому фразами: "память от 16 ГБ", " процессор Ryzen, но НЕ серии U", "Android не iOS".
- НЕ задавай вопросы про города и про Авито Доставку — бот спросит сам отдельным вопросом с кнопками."""


async def plan_request(raw_text: str, default_cities: list) -> dict | None:
    prompt = (
        f"{PROFILE_SYSTEM}\n\n"
        f"Запрос пользователя: «{raw_text}»\n"
        f"Если города не указаны — возьми города по умолчанию: {json.dumps(default_cities, ensure_ascii=False)}."
    )
    for attempt in (1, 2):
        try:
            text = await _beatapi(prompt, "plan")
        except Exception as e:
            log.info("Ошибка plan_request: %s", e)
            text = ""
        plan = _extract_json(text)
        if isinstance(plan, dict):
            break
        log.info("план не распарсился (попытка %s), повтор...", attempt)
        await asyncio.sleep(10)
    else:
        return None
    if not isinstance(plan, dict):
        return None
    # нормализация
    plan.setdefault("title", "Поиск")
    plan.setdefault("category_slug", None)
    plan.setdefault("title_query", "")
    queries = plan.get("search_queries")
    if not isinstance(queries, list) or not queries:
        queries = [plan.get("title_query") or plan.get("title", "")] if (plan.get("title_query") or plan.get("title")) else [""]
    plan["search_queries"] = [str(q).strip() for q in queries if str(q).strip()][:5]
    plan.setdefault("city_slugs", default_cities)
    plan.setdefault("price_min", None)
    plan.setdefault("price_max", None)
    plan.setdefault("must", [])
    plan.setdefault("must_not", [])
    plan.setdefault("nice", [])
    plan.setdefault("clarify", [])
    return plan


async def merge_answers(profile: dict, answers: list) -> dict | None:
    if not answers:
        return profile
    prompt = (
        f"{PROFILE_SYSTEM}\n\n"
        f"Текущий профиль поиска:\n{json.dumps(profile, ensure_ascii=False, indent=2)}\n\n"
        "Ответы пользователя на уточняющие вопросы:\n"
        f"{json.dumps(answers, ensure_ascii=False, indent=2)}\n\n"
        "Верни ОБНОВЛЁННЫЙ профиль в том же JSON-формате (title, category_slug, search_queries, "
        "title_query, city_slugs, price_min, price_max, must, must_not, nice). Сохрани всё, что "
        "уже есть, и зачти ответы: если ответ меняет бренды/модели/характеристики — обнови и "
        "search_queries под новые требования. clarify верни пустым []."
    )
    try:
        text = await _beatapi(prompt, "merge")
    except Exception as e:
        log.info("Ошибка merge_answers: %s", e)
        return None
    merged = _extract_json(text)
    if not isinstance(merged, dict):
        return None
    merged.pop("clarify", None)
    return merged


# --------------------------------------------------------------- фидбэк

FEEDBACK_SYSTEM = """Ты — AI-помощник покупателя. Пользователь ответил (reply) на уведомление о найденном лоте и объяснил, почему лот ему не подходит (или что искать нужно иначе/заново).

На вход даются: профиль поиска, информация о лоте и текст пользователя.

Верни СТРОГО JSON:
{
  "add_must": ["новые ЖЁСТКИЕ требования, выведенные из текста (если есть)"],
  "add_must_not": ["новые ИСКЛЮЧЕНИЯ (если есть), например 'ноутбуки soll ядер без видеокарты'"],
  "price_min": число или null,
  "price_max": число или null,
  "note": "короткая пометка, если она важна для дальнейшего поиска, иначе пустая строка",
  "action": "none" | "pause" | "stop" (если человек просит остановить/остановить поиск — стоп, приостановить — пауза),
  "reply": "короткий ответ юзеру (1-2 строки): что учли"
}

Не выдумывай требований, которых пользователь не сказал. Если текст невозможно разобрать — верни пустые списки и reply с просьбой уточнить."""


async def parse_feedback(profile: dict, item_info: str, feedback_text: str) -> dict | None:
    prompt = (
        f"{FEEDBACK_SYSTEM}\n\n"
        f"Профиль поиска:\n{json.dumps(profile, ensure_ascii=False, indent=2)}\n\n"
        f"Лот из уведомления:\n{item_info}\n\n"
        f"Текст пользователя:\n«{feedback_text}»"
    )
    try:
        text = await _beatapi(prompt, "feedback")
    except Exception as e:
        log.info("Ошибка parse_feedback: %s", e)
        return None
    upd = _extract_json(text)
    if not isinstance(upd, dict):
        return None
    upd.setdefault("add_must", [])
    upd.setdefault("add_must_not", [])
    upd.setdefault("price_min", None)
    upd.setdefault("price_max", None)
    upd.setdefault("note", "")
    upd.setdefault("action", "none")
    upd.setdefault("reply", "")
    return upd


# --------------------------------------------------------------- скрининг листингов

SCREEN_SYSTEM = """Ты — фильтр покупателя на Авито. Даны поиски (их профили) и списки НОВЫХ лотов для каждого. Задача: ОТБРАКОВАТЬ лоты, которые НЕУЖ точно не подойдут, и оставить только кандидатов для детальной проверки.

Верни СТРОГО JSON массив:
[{"item_id": "...", "search_id": <число>, "screen": "YES" | "NO", "why": "короткое объяснение"}]

Каждый лот из входных данных должен попасть в ответ. Правила:
- Не ставь NO только потому, что в названии мало подробностей — детальную проверку делает следующий этап. NO ставь только при ЯВНОМ несоответствии: не та категория товара, бренд из must_not, цена сильно вне бюджета, очевидный брак ("на запчасти", "разбит", "залит").
- must-требования на этом этапе проверяй только самые явные. Если данных мало — ставь YES."""


async def screen_items(tasks: list) -> dict:
    """tasks: [{"search_id": sid, "profile": {...}, "items": [{"item_id", "title", "price", "snippet"}]}]
    Возвращает {(search_id, item_id): ("YES"/"NO", "reason")}."""
    out = {}
    if not tasks:
        return out
    slim = []
    for t in tasks:
        slim.append({
            "search_id": t["search_id"],
            "profile": {
                "title": t["profile"].get("title", ""),
                "price_min": t["profile"].get("price_min"),
                "price_max": t["profile"].get("price_max"),
                "must": t["profile"].get("must", []),
                "must_not": t["profile"].get("must_not", []),
            },
            "items": t["items"],
        })
    prompt = f"{SCREEN_SYSTEM}\n\nДанные:\n{json.dumps(slim, ensure_ascii=False, indent=1)}"
    try:
        text = await _beatapi(prompt, "screen")
    except Exception as e:
        log.info("Ошибка screen_items: %s", e)
        return out
    rows = _extract_json(text)
    if isinstance(rows, dict):
        rows = rows.get("items") or rows.get("results") or []
    if not isinstance(rows, list):
        rows = _lenint_records(text)
    if not isinstance(rows, list):
        return out
    for r in rows:
        if not isinstance(r, dict):
            continue
        sid = r.get("search_id") or r.get("sudba_id")
        iid = str(r.get("item_id", ""))
        verdict = str(r.get("screen", "NO")).upper()
        if verdict not in ("YES", "NO"):
            verdict = "NO"
        out[(sid, iid)] = (verdict, str(r.get("why", "")))
    return out


# --------------------------------------------------------------- сверка карточек

VERIFY_SYSTEM = """Ты — эксперт-сверщик характеристик товаров Авито. Даны поиски (их профили с требованиями must/must_not/nice) и карточки лотов (название, цена, полное описание, список характеристик с Авито).

Задача: сверить каждую карточку с требованиями профиля и вернуть вердикт.

Верни СТРОГО JSON массив:
[{"item_id": "...", "search_id": <число>, "verdict": "MATCH" | "CLOSE" | "REJECT", "model": "распознанная модель/конфигурация",
  "reason": "почему подходит/не подходит — со ссылкой на конкретные требования и данные карточки",
  "tips": "что проверить у продавца перед покупкой (3-6 пунктов кратко)"}]

Правила:
- MATCH: ВСЕ must-требования подтверждены данными карточки и ни одно из must_not не задето.
- CLOSE: как будто подходит, но какого-то must-требования НЕ УДОСОВЕРИТЬ по карточке (данных нет), либо не совпало нечто важное на твое усмотрение.
- REJECT: нарушен любой must или задет любой must_not, либо товар с явным дефектом (битый экран, на запчасти, залит, не включается и т.п.) — даже если это в must_not не вписано.
- Не подгоняй данные под требования: если в карточке 8 ГБ памяти, а нужно 16 — REJECT, а не MATCH.
- Если в карточке нет противоречий, но данных мало — CLOSE и в reason укажи, чего не хватает.
- Каждый лот из входных данных должен попасть в ответ.
- ВАЖНО: не используй двойные кавычки " и апострофы ' внутри значений полей — это ломает JSON."""


async def verify_items(tasks: list) -> list:
    """tasks: [{"search_id": sid, "profile": {...}, "cards": [{"item_id", "title", "price", "description", "params"}]}]
    Возвращает список вердиктов."""
    rows = []
    if not tasks:
        return rows
    slim = []
    for t in tasks:
        slim.append({
            "search_id": t["search_id"],
            "profile": {
                "title": t["profile"].get("title", ""),
                "price_min": t["profile"].get("price_min"),
                "price_max": t["profile"].get("price_max"),
                "must": t["profile"].get("must", []),
                "must_not": t["profile"].get("must_not", []),
                "nice": t["profile"].get("nice", []),
            },
            "cards": t["cards"],
        })
    prompt = f"{VERIFY_SYSTEM}\n\nДанные:\n{json.dumps(slim, ensure_ascii=False, indent=1)}"
    try:
        text = await _beatapi(prompt, "verify")
    except Exception as e:
        log.info("Ошибка verify_items: %s", e)
        return rows
    parsed = _extract_json(text)
    if isinstance(parsed, dict):
        parsed = parsed.get("items") or parsed.get("results") or []
    if not isinstance(parsed, list):
        parsed = _lenint_records(text)
    if not isinstance(parsed, list):
        return rows
    for r in parsed:
        if not isinstance(r, dict):
            continue
        rows.append({
            "item_id": str(r.get("item_id", "")),
            "search_id": r.get("search_id") or r.get("sudba_id"),
            "verdict": str(r.get("verdict", "REJECT")).upper(),
            "model": str(r.get("model", "")),
            "reason": str(r.get("reason", "")),
            "tips": str(r.get("tips", "")),
        })
    return rows
