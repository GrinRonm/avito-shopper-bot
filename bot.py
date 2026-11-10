"""
Avito Shopper Bot — телеграм-бот, который ищет на Авито ЛЮБОЙ товар по
свободному текстовому запросу, уточняет фильтры кнопками, сверяет
характеристики карточек и присылает совпадения. Ответы-реплаи на
уведомления корректируют дальнейший поиск.

Запуск:  python bot.py
"""
import asyncio
import html
import logging
import os
import sys

# безопасный вывод на Windows-консоли
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import ai as ai_client
import appconfig
import avito
import categories  # noqa: F401 (используется city_label)
import db
import searchflow
from categories import CATEGORIES

log = logging.getLogger("bot")

TOKEN = appconfig.cfg("telegram_bot_token")
OWNERS = set(appconfig.cfg("allowed_chat_ids", []))   # владельцы (подтверждают заявки)

BOT: Bot | None = None               # доступен webapp.py (заявки из мини-аппа)
_wake = asyncio.Event()          # будим скан-цикл досрочно (новый поиск)
pending: dict[int, dict] = {}    # chat_id -> {"sid": int, "idx": int}

router = Router()


# ============================================================ утилиты

def allowed(chat_id: int) -> bool:
    return db.is_allowed(chat_id)


def rub(v) -> str:
    try:
        return f"{int(v):,}".replace(",", " ") + " ₽"
    except Exception:
        return str(v)


def esc(s: str) -> str:
    return html.escape(str(s or ""))


def bullets(items: list) -> str:
    return "".join(f"   • {esc(x)}\n" for x in items)


def profile_text(profile: dict) -> str:
    pmin, pmax = profile.get("price_min"), profile.get("price_max")
    price = ""
    if pmin or pmax:
        price = f"\n💰 Цена: {rub(pmin) if pmin else '—'} — {rub(pmax) if pmax else '—'}"
    cat = profile.get("category_slug")
    cat_line = f"\n📂 Категория: {CATEGORIES.get(cat, cat or 'поиск по всей Авито')}"
    cities = profile.get("city_slugs") or []
    out = f"{cat_line}\n🌍 Где: {', '.join(cities)}{price}"
    if profile.get("must"):
        out += f"\n✅ Обязательно:\n{bullets(profile['must'])}"
    if profile.get("must_not"):
        out += f"\n🚫 Исключения:\n{bullets(profile['must_not'])}"
    if profile.get("nice"):
        out += f"\n✨ Желательно:\n{bullets(profile['nice'])}"
    return out


STATUS_EMOJI = {"ACTIVE": "🟢", "PAUSED": "⏸", "STOPPED": "🛑", "ASKING": "❓", "DONE": "✅"}


def notif_kb(sid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏸ Пауза поиска", callback_data=f"ntf:pause:{sid}"),
         InlineKeyboardButton(text="🛑 Завершить поиск", callback_data=f"ntf:stop:{sid}")],
    ])


# ============================================================ команды

WELCOME = (
    "👋 Привет! Я — Авито-поискатель под тебя.\n\n"
    "Просто напиши, что ищешь, как ты это формулируешь, например:\n"
    "<i>«Найди мне телефон, нужен на андроиде»</i>\n"
    "<i>«найди мне ноутбук память 16/512 и выше, бренды asus, redmi, цена от 40к до 70к, "
    "процессоры на ryzen но не учитывай U-серии»</i>\n\n"
    "Я задам пару уточняющих вопросов кнопками и начну мониторить Авито.\n"
    "Когда найду что-то подходящее — пришлю уведомление.\n"
    "💬 Если что-то не подходит — <b>ответь (reply) на то сообщение</b> и напиши, что не так: "
    "я учту это в дальнейшем поиске.\n\n"
    "🔍 Мои активные поиски: /searches"
)


@router.message(CommandStart())
async def cmd_start(message: Message):
    if message.chat.type != "private":
        return
    if not allowed(message.chat.id):
        await request_access(message.bot, message.chat.id, message.from_user)
        return
    await message.answer(WELCOME, disable_web_page_preview=True, reply_markup=web_kb())


@router.message(Command("searches"))
async def cmd_searches(message: Message):
    if not allowed(message.chat.id):
        return
    await send_searches_list(message.bot, message.chat.id)


def web_domain() -> str:
    return (str(appconfig.cfg("web_domain", "") or "")).strip().rstrip("/")


def web_kb() -> InlineKeyboardMarkup | None:
    domain = web_domain()
    if not domain.startswith("https://"):
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🌐 Открыть веб-приложение", web_app=_tg_webapp()),
    ]])


def _tg_webapp():
    from aiogram.types import WebAppInfo
    return WebAppInfo(url=web_domain())


@router.message(Command("link"))
async def cmd_link(message: Message):
    if message.chat.type != "private" or not allowed(message.chat.id):
        return
    domain = web_domain()
    if not domain:
        await message.answer("🌐 Сайт ещё не настроен — уточни у владельца.")
        return
    code = db.create_auth_code(message.chat.id)
    await message.answer(
        f"🔑 Код для входа на {domain}:\n<code>{code}</code>\n\n"
        f"Действует 10 минут, одноразовый. Если открыл сайт из кнопки мини-аппа — вход произойдёт автоматически.",
        disable_web_page_preview=True,
    )


# ============================================================ заявки на доступ

async def request_access(bot: Bot, chat_id: int, user):
    """Незнакомый юзер — заявка владельцам на одобрение."""
    name = (user.first_name or "") + (" " + user.last_name if user.last_name else "")
    username = f"@{user.username}" if user.username else ""
    try:
        await bot.send_message(chat_id, "👋 Бот личный — отправил заявку владельцу. Ожидай одобрения.")
    except Exception:
        pass
    await request_access_now(bot, chat_id, username, name)


async def request_access_now(bot: Bot, chat_id: int, username: str, name: str = "", last_name: str = ""):
    """Заявка владельцам; вызывается и из mini-app (webapp.py)."""
    if last_name:
        name = (name + " " + last_name).strip()
    is_new = db.upsert_access_request(chat_id, username, name)
    if not is_new:
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Дать доступ", callback_data=f"acc:approve:{chat_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"acc:deny:{chat_id}"),
    ]])
    for owner in OWNERS:
        try:
            await bot.send_message(
                owner,
                f"🚪 <b>Заявка на доступ</b>\nИмя: {esc(name)}\nТег: {esc(username)}\nchat_id: <code>{chat_id}</code>",
                reply_markup=kb,
            )
        except Exception:
            log.info("не смогли доставить заявку владельцу %s", owner)


@router.callback_query(F.data.startswith("acc:"))
async def on_access_decision(call: CallbackQuery):
    _, act, cid_s = call.data.split(":")
    cid = int(cid_s)
    if call.from_user.id not in OWNERS:
        await call.answer("Только владелец", show_alert=True)
        return
    req = db.get_access_request(cid) or {}
    if act == "approve":
        db.add_allowed(cid, req.get("username", ""), req.get("name", ""))
        try:
            await call.bot.send_message(cid, "✅ Владелец дал тебе доступ! Напиши, что искать.")
        except Exception:
            pass
        await call.answer("Доступ выдан")
        text = f"✅ <b>Доступ выдан</b> {esc(req.get('username') or req.get('name') or cid)}"
    else:
        db.deny_user(cid)
        await call.answer("Отклонено")
        text = f"❌ <b>Заявка отклонена</b> {esc(req.get('username') or req.get('name') or cid)}"
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await call.message.answer(text)


async def send_searches_list(bot: Bot, chat_id: int):
    rows = db.list_searches_by_chat(chat_id)
    if not rows:
        await bot.send_message(chat_id, "Пока поисков нет. Напиши, что искать 🙂")
        return
    lines, kb_rows = [], []
    for s in rows[:10]:
        emoji = STATUS_EMOJI.get(s["status"], "•")
        pmin = s["profile"].get("price_min")
        pmax = s["profile"].get("price_max")
        price = ""
        if pmin or pmax:
            price = f" ({rub(pmin) if pmin else '0'}–{rub(pmax) if pmax else '∞'})"
        lines.append(f"{emoji} #{s['id']} «{esc(s['title'] or 'Без названия')}»{price}")
        if s["status"] == "ACTIVE":
            acts = [("⏸", "pause"), ("🛑", "stop")]
        elif s["status"] == "PAUSED":
            acts = [("▶️", "resume"), ("🛑", "stop")]
        elif s["status"] == "ASKING":
            acts = [("🛑", "stop")]
        else:
            acts = []
        if acts:
            kb_rows.append([InlineKeyboardButton(text=f"{t} #{s['id']}", callback_data=f"mng:{a}:{s['id']}") for t, a in acts])
    if web_kb():
        kb_rows = kb_rows + [[InlineKeyboardButton(text="🌐 Веб-приложение (лоты, фильтры)", web_app=_tg_webapp())]]
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows) if kb_rows else None
    await bot.send_message(chat_id, "\n".join(lines), reply_markup=kb, disable_web_page_preview=True)


@router.callback_query(F.data.startswith("mng:"))
async def on_manage(call: CallbackQuery):
    _, action, sid_s = call.data.split(":")
    sid = int(sid_s)
    if not allowed(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    s = db.get_search(sid)
    if not s:
        await call.answer("Поиск не найден", show_alert=True)
        return
    if action == "pause":
        db.set_search_status(sid, "PAUSED")
        await call.answer("⏸ Пауза")
    elif action == "resume":
        db.set_search_status(sid, "ACTIVE")
        _wake.set()
        await call.answer("▶️ Продолжаю")
    elif action == "stop":
        db.set_search_status(sid, "STOPPED")
        pending.pop(call.from_user.id, None)
        await call.answer("🛑 Остановлен")
    if call.message:
        try:
            await call.message.delete()
        except Exception:
            pass
    await send_searches_list(call.bot, call.from_user.id)


@router.callback_query(F.data.startswith("ntf:"))
async def on_notif_button(call: CallbackQuery):
    _, action, sid_s = call.data.split(":")
    sid = int(sid_s)
    if not allowed(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    db.set_search_status(sid, "PAUSED" if action == "pause" else "STOPPED")
    if call.message:
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    if action == "pause":
        await call.message.answer(f"⏸ Поиск #{sid} на паузе. Вернём: /searches")
        await call.answer("Пауза")
    else:
        await call.message.answer(f"🛑 Поиск #{sid} завершён.")
        await call.answer("Завершено")


# ============================================================ диалог уточнений

def city_label(slug: str) -> str:
    for name, s in categories.CITIES.items():
        if s == slug:
            return name.capitalize()
    return slug


def cities_options(plan: dict) -> list:
    default_cities = appconfig.cfg("default_city_slugs", ["novokuznetsk"])
    plan_cities = [c for c in (plan.get("city_slugs") or [])]
    same = sorted(map(str, plan_cities)) == sorted(map(str, default_cities))
    opts = []
    if not same:
        opts.append(f"{', '.join(city_label(c) for c in plan_cities)} (как в запросе)")
    opts.append(f"{', '.join(city_label(c) for c in default_cities)} (по умолчанию)")
    opts.append("Вся Россия + Авито Доставка")
    opts.append("✍️ Другие города — напишу текстом")
    return opts


async def record_answer_and_next(bot: Bot, chat_id: int, sid: int, idx: int, answer: str):
    s = db.get_search(sid)
    if not s or idx >= len(s["questions"]):
        return
    q = s["questions"][idx]
    db.append_answer(sid, {"question": q.get("question", ""), "answer": answer})

    # города не по умолчанию -> сразу уточняем Авито Доставку
    if q.get("id") == "cities" and "(по умолчанию)" not in answer and "Доставка" not in answer:
        questions = list(s["questions"])
        if not any(qq.get("id") == "delivery" for qq in questions):
            questions.insert(idx + 1, {
                "id": "delivery",
                "question": "Авито Доставка нужна?",
                "options": ["Только с Авито Доставкой", "Только обычные объявления", "Без разницы"],
            })
            db.update_questions(sid, questions)

    await ask_current_question(bot, chat_id, sid)


def question_kb(sid: int, q_index: int, options: list) -> InlineKeyboardMarkup:
    rows = []
    for i, opt in enumerate(options):
        rows.append([InlineKeyboardButton(text=opt[:64], callback_data=f"ans:{sid}:{q_index}:{i}")])
    rows.append([InlineKeyboardButton(text="✍️ Написать свой вариант", callback_data="freetext")])
    rows.append([InlineKeyboardButton(text="⏭ Пропустить остальные", callback_data=f"skip:{sid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def ask_current_question(bot: Bot, chat_id: int, sid: int):
    s = db.get_search(sid)
    if not s or s["status"] != "ASKING":
        pending.pop(chat_id, None)
        return
    idx = len(s["answers"])
    questions = s["questions"]
    if idx >= len(questions):
        pending.pop(chat_id, None)
        await finalize_search(bot, chat_id, sid)
        return
    q = questions[idx]
    pending[chat_id] = {"sid": sid, "idx": idx}
    kb = question_kb(sid, idx, q.get("options", []))
    await bot.send_message(chat_id, f"❓ {esc(q.get('question', ''))}", reply_markup=kb)


async def finalize_search(bot: Bot, chat_id: int, sid: int):
    s = db.get_search(sid)
    if not s:
        return
    profile, answers = s["profile"], s["answers"]
    if answers:
        await bot.send_message(chat_id, "🧠 Учну требования с учётом ответов...")
        merged = await ai_client.merge_answers(profile, answers)
        if merged:
            profile = merged
        else:
            await bot.send_message(chat_id, "⚠️ Не смог уточнить профиль через ИИ — ищу по начальным требованиям.")
    db.update_search_profile(sid, profile, answers)
    db.set_search_status(sid, "ACTIVE")
    pending.pop(chat_id, None)
    _wake.set()
    await bot.send_message(
        chat_id,
        f"🚀 Запускаю поиск #{sid} «{esc(s['title'])}»\n{profile_text(profile)}\n\n"
        "🔎 Сканирую Авито, уведомлю при совпадении.",
        disable_web_page_preview=True,
    )


@router.callback_query(F.data.startswith("ans:"))
async def on_q_answer(call: CallbackQuery):
    _, sid_s, idx_s, opt_s = call.data.split(":")
    sid, idx, oi = int(sid_s), int(idx_s), int(opt_s)
    chat_id = call.from_user.id
    if not allowed(chat_id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    pend = pending.get(chat_id)
    if not pend or pend["sid"] != sid or pend["idx"] != idx:
        await call.answer("Неактуальный вопрос", show_alert=True)
        return
    s = db.get_search(sid)
    if not s or idx >= len(s["questions"]):
        await call.answer("Ошибка вопроса", show_alert=True)
        return
    options = s["questions"][idx].get("options", [])
    answer = options[oi] if oi < len(options) else ""
    await call.answer(f"Принял: {answer[:30]}")
    if call.message:
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    await record_answer_and_next(call.bot, chat_id, sid, idx, answer)


@router.callback_query(F.data == "freetext")
async def on_freetext(call: CallbackQuery):
    await call.answer("Напиши ответ обычным сообщением 👇", show_alert=False)


@router.callback_query(F.data.startswith("skip:"))
async def on_skip(call: CallbackQuery):
    sid = int(call.data.split(":")[1])
    chat_id = call.from_user.id
    if not allowed(chat_id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    await call.answer("Ок, ищу по тому, что есть")
    if call.message:
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    pending.pop(chat_id, None)
    await finalize_search(call.bot, chat_id, sid)


# ============================================================ текст пользователя

@router.message(F.text)
async def on_text(message: Message):
    chat_id = message.chat.id
    if message.chat.type != "private":
        return
    if not allowed(chat_id):
        await request_access(message.bot, chat_id, message.from_user)
        return
    text = (message.text or "").strip()
    if not text:
        return

    # 1) reply на уведомление -> фидбэк
    if message.reply_to_message:
        notif = db.get_notification(message.reply_to_message.message_id)
        if notif and notif["chat_id"] == chat_id:
            await handle_feedback(message, notif, text)
            return

    # 2) ответ на уточняющий вопрос текстом
    pend = pending.get(chat_id)
    if pend:
        sid, idx = pend["sid"], pend["idx"]
        s = db.get_search(sid)
        if s and idx < len(s["questions"]) and s["status"] == "ASKING":
            await message.answer(f"✍️ Принял: «{esc(text[:60])}»")
            await record_answer_and_next(message.bot, chat_id, sid, idx, text)
            return
        pending.pop(chat_id, None)

    # 3) незавершённый asking-поиск (после перезапуска бота)
    asking = db.list_asking_searches(chat_id)
    if asking:
        await message.answer("⏳ У нас остался незавершённый поиск — дособеру уточнения.")
        await ask_current_question(message.bot, chat_id, asking[0])
        return

    # 4) новый запрос
    await handle_new_request(message, text)


async def handle_new_request(message: Message, text: str):
    # нельзя вести два поиска одновременно
    if searchflow.has_running_dialog(message.chat.id):
        await message.answer("🚫 Один поиск уже запущен или уточняется — начнём новый после него.")
        asking = db.list_asking_searches(message.chat.id)
        if asking:
            await ask_current_question(message.bot, message.chat.id, asking[0])
        return

    sid = db.create_search(message.chat.id, "Формируется…", text, {"raw": text}, [])
    db.set_search_status(sid, "PLANNING")
    default_cities = appconfig.cfg("default_city_slugs", ["rossiya"])
    asyncio.create_task(_plan_and_ask(message.bot, message.chat.id, sid, text, default_cities))
    await message.answer(
        f"🧠 Делаю план поиска…\nЗапросы к нейросети встают в очередь (лимит ~63 сек) — это может занять пару минут.",
    )


async def _plan_and_ask(bot: Bot, chat_id: int, sid: int, text: str, default_cities: list):
    ok = await searchflow.plan_search(sid, text, default_cities)
    if ok:
        await ask_current_question(bot, chat_id, sid)
    else:
        pending.pop(chat_id, None)
        try:
            await bot.send_message(
                chat_id,
                f"😵 Не смог собрать план для «{esc(text[:120])}». Попробуй сформулировать иначе или нажми 🔄 Заново в /searches.",
            )
        except Exception:
            pass


async def handle_feedback(message: Message, notif: dict, text: str):
    sid = notif["search_id"]
    search = db.get_search(sid)
    if not search:
        await message.answer("Этот поиск уже не существует.")
        return

    thinking = await message.answer("🧠 Понял, учту...")
    upd = await ai_client.parse_feedback(search["profile"], notif["summary"], text)
    if not upd:
        await thinking.edit_text("😵 Не разобрал ответ. Напиши чуть подробнее, почему не подходит.")
        return

    # лот, отклонённый реплаем, прячем навсегда: он не зайдёт на сайт и не всплывёт снова
    db.hide_item_forever(notif["search_id"], notif["avito_id"],
                         reason=f"Отклонён реплаем: {(text or '—')[:120]}")

    profile = search["profile"]
    profile.setdefault("must", []).extend(upd.get("add_must", []))
    profile.setdefault("must_not", []).extend(upd.get("add_must_not", []))
    if upd.get("price_min"):
        profile["price_min"] = upd["price_min"]
    if upd.get("price_max"):
        profile["price_max"] = upd["price_max"]
    if upd.get("note"):
        profile.setdefault("notes", []).append(upd["note"])
    db.update_search_profile(sid, profile)

    action = upd.get("action", "none")
    if action == "stop":
        db.set_search_status(sid, "STOPPED")
    elif action == "pause":
        db.set_search_status(sid, "PAUSED")

    changes = []
    for x in upd.get("add_must", []):
        changes.append(f"✅ теперь обязательно: {esc(x)}")
    for x in upd.get("add_must_not", []):
        changes.append(f"🚫 больше не искать: {esc(x)}")
    msg = "🧠 <b>Учёл!</b>\n" + "\n".join(changes) if changes else (f"🧠 {esc(upd.get('reply', 'Учёл.'))}")
    if action == "stop":
        msg += "\n\n🛑 Поиск остановлен."
    elif action == "pause":
        msg += "\n\n⏸ Поиск на паузе."
    await thinking.edit_text(msg, disable_web_page_preview=True)


# ============================================================ скан-цикл

async def run_scan(bot: Bot):
    sids = db.list_active_searches()
    if not sids:
        return
    searches = [db.get_search(s) for s in sids if db.get_search(s)]
    if not searches:
        return

    log.info(f"🔎 Скан: {len(searches)} активных поисков")
    scraped: dict[int, list] = {}
    try:
        pw, browser, page = await asyncio.wait_for(avito.open_browser_context(), timeout=40)
    except Exception:
        log.exception("не удалось открыть браузер")
        return
    try:
        for s in searches:
            s["_first_pass"] = s["last_scan_at"] is None
            try:
                scraped[s["id"]] = await asyncio.wait_for(avito.scan_listing(page, s), timeout=600)
                db.mark_scanned(s["id"])
            except Exception:
                log.exception("scan error for search %s", s["id"])
                scraped[s["id"]] = []
    finally:
        try:
            await asyncio.wait_for(avito.close_browser_context(pw, browser), timeout=20)
        except Exception:
            log.exception("браузер не закрылся чисто")

    # 1) скрининг заголовков (1 вызов нейросети на весь цикл)
    tasks = [
        {"search_id": s["id"], "profile": s["profile"], "items": scraped[s["id"]]}
        for s in searches
        if scraped[s["id"]]
    ]
    screened = await ai_client.screen_items(tasks)

    inspect_pool: list[tuple[dict, dict]] = []
    for s in searches:
        for it in scraped[s["id"]]:
            verdict, why = screened.get((s["id"], it["item_id"]), ("NO", "Нет ответа нейросети"))
            if verdict == "YES":
                db.update_item(s["id"], it["item_id"], status="SCREENED_YES")
                inspect_pool.append((s, it))
            else:
                db.update_item(s["id"], it["item_id"], status="SCREENED_NO", reason=why)
                log.info(f"   [отсев] {it['title'][:40]} -> {why}")

    # 2) заход в карточки (лимит на цикл), локальная проверка дефектов
    cap = appconfig.cfg("max_card_inspections_per_cycle", 4)
    inspected: dict[tuple, dict] = {}
    if inspect_pool:
        pw, browser, page = await avito.open_browser_context()
        try:
            i = 0
            while inspect_pool and i < cap:
                s, it = inspect_pool.pop(0)
                info = await avito.inspect_item(page, it["url"])
                if info["is_reserved"]:
                    db.update_item(s["id"], it["item_id"], status="RESERVED", reason="Товар зарезервирован")
                    continue
                if info["defect"]:
                    db.update_item(s["id"], it["item_id"], status="REJECTED_LOCAL", reason=f"Дефект: {info['defect']}")
                    log.info(f"   [дефект] {it['title'][:40]} -> {info['defect']}")
                    continue
                if not info["description"] and not info["params"]:
                    # не смогли снять ничего с карточки — уведомлять бессмысленно
                    db.update_item(s["id"], it["item_id"], status="NO_DATA",
                                   reason="Карточка пуста: не удалось снять описание/характеристики")
                    log.info(f"   [нет данных] {it['title'][:40]} -> карточка не открылась с данными")
                    continue
                inspected[(s["id"], it["item_id"])] = {"search": s, "item": it, "card": info}
                # кэшируем карточку целиком (описание, характеристики, фото)
                db.save_item_card(s["id"], it["item_id"], info)
                i += 1
        finally:
            await avito.close_browser_context(pw, browser)

    # 3) сверка характеристик с профилем (1 вызов нейросети на весь цикл)
    vtasks = []
    for (sid, iid), pack in inspected.items():
        s, it, card = pack["search"], pack["item"], pack["card"]
        vtasks.append({
            "search_id": sid,
            "profile": s["profile"],
            "cards": [{
                "item_id": iid,
                "title": it["title"],
                "price": it["price"],
                "description": card["description"],
                "params": card["params"],
            }],
        })
    verdicts = await ai_client.verify_items(vtasks)

    max_notif = appconfig.cfg("max_notifications_per_cycle", 5)
    notify_close = appconfig.cfg("notify_close_matches", True)
    notifications = 0
    item_map = {(s["id"], it["item_id"]): {"search": s, "item": it, "card": card}
                for (sid, iid), pack in inspected.items()
                for s, it, card in [(pack["search"], pack["item"], pack["card"])]}

    for v in verdicts:
        sid = v.get("search_id")
        iid = v.get("item_id")
        key = (sid, iid)
        pack = item_map.get(key)
        if not pack:
            continue
        verdict = v.get("verdict", "REJECT")
        db.update_item(sid, iid, status=f"VERIFIED_{verdict}",
                       verdict=verdict, reason=v.get("reason", ""), ai_json=v.get("model", ""))
        if verdict == "REJECT":
            log.info(f"   [REJECT] {pack['item']['title'][:40]} -> {v.get('reason', '')[:80]}")
            continue
        if verdict == "CLOSE" and not notify_close:
            continue
        if notifications >= max_notif:
            continue
        notifications += 1
        await send_match_notification(bot, pack, v)

    if notifications == 0:
        log.info("   За этот круг подходящих не найдено")


async def send_match_notification(bot: Bot, pack: dict, verdict: dict):
    s, it, card = pack["search"], pack["item"], pack["card"]
    sid = s["id"]
    model = verdict.get("model") or it["title"]
    is_close = verdict.get("verdict") == "CLOSE"
    head = "🟡 <b>Возможно подойдёт</b>" if is_close else "✅ <b>Найдено под твой запрос!</b>"

    parts = [
        f"{head}\n<b>Поиск:</b> #{sid} «{esc(s['title'])}»",
        f"\n📦 <b>{esc(model)}</b>",
        f"💰 Цена: {rub(it['price'])}",
        f"\n🧐 <b>Почему подходит:</b> {esc(verdict.get('reason', ''))}",
    ]
    if verdict.get("tips"):
        parts.append(f"⚠️ <b>Что проверить:</b>\n{esc(verdict['tips'])}")
    parts.append(f"\n🔗 <a href=\"{it['url']}\">Открыть на Авито</a>")
    parts.append("\n💬 Если не подходит — ответь (reply) на это сообщение и напиши почему.")

    try:
        msg = await bot.send_message(
            s["chat_id"],
            "\n".join(parts),
            reply_markup=notif_kb(sid),
            disable_web_page_preview=False,
        )
        summary = (f"{model} · {rub(it['price'])} · {verdict.get('reason', '')} · {it['url']}")
        db.add_notification(msg.message_id, sid, s["chat_id"], it["item_id"], summary)
        db.update_item(sid, it["item_id"], status="NOTIFIED", notified=1)
        log.info(f"   🔥 Уведомление: {model} ({rub(it['price'])})")
    except Exception:
        log.exception("ошибка отправки уведомления")


# ============================================================ main

async def scan_loop(bot: Bot):
    interval = appconfig.cfg("check_interval_seconds", 120)
    while True:
        try:
            try:
                await asyncio.wait_for(_wake.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            _wake.clear()
            await run_scan(bot)
            if avito.pop_throttled():
                # Avito режет IP: не дёргаем его по расписанию, ставим паузу побольше,
                # так бан спадает быстрее (серия: 600с, 900с, 1200с... максимум ~25 мин)
                scan_loop._streak = getattr(scan_loop, "_streak", 0) + 1
                extra = min(300 + scan_loop._streak * 300, 1500)
                log.info("⏳ Пауза дополнительно %s сек — даём IP передышку", extra)
                await asyncio.sleep(extra)
            else:
                scan_loop._streak = 0
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Скан-цикл упал, рестарт через 30 сек")
            await asyncio.sleep(30)


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.log"), encoding="utf-8")],
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)

    db.init_db()
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    globals()["BOT"] = bot          # webapp.py шлёт заявки через BOT
    dp = Dispatcher()
    dp.include_router(router)

    scanner = asyncio.create_task(scan_loop(bot))

    web_runner = None
    try:
        import webapp
        web_runner = await webapp.start_server()
    except Exception:
        log.exception("не удалось поднять mini-app сайт (бот продолжит работать)")

    log.info("🤖 Avito Shopper Bot запущен")
    try:
        await dp.start_polling(bot)
    finally:
        scanner.cancel()
        try:
            await scanner
        except asyncio.CancelledError:
            pass
        if web_runner:
            try:
                await web_runner.cleanup()
            except Exception:
                pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Остановлено пользователем")
