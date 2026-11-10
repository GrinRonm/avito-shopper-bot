"""Mini-app сайт: aiohttp-сервер.
Авторизация: подпись Telegram WebApp initData либо одноразовый код из бота (/link).
Лоты (фильтры по поиску/статусам, избранное, скрытие, автообновление),
управление поисками, создание поиска с уточнением кнопками и
режим «Сравнение»: 2-6 лотов -> карточки с Авито -> вердикт нейросети.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import time
import urllib.parse

from aiohttp import web

import appconfig
import db
import searchflow

log = logging.getLogger("webapp")

COOKIE = "shoppersess"


# ============================================================ сессии

def _secret() -> bytes:
    return (str(appconfig.cfg("beatapi_key", "")) + str(appconfig.cfg("telegram_bot_token", ""))).encode()


def _sign(payload: str) -> str:
    return hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()


def make_token(chat_id: int) -> str:
    exp = int(time.time()) + 60 * 60 * 24 * 60  # 60 дней
    payload = f"{chat_id}.{exp}"
    return f"{payload}.{_sign(payload)}"


def verify_token(token: str) -> int | None:
    try:
        chat_id_s, exp_s, sig = token.split(".")
        payload = f"{chat_id_s}.{exp_s}"
        if not hmac.compare_digest(sig, _sign(payload)):
            return None
        if int(exp_s) < time.time():
            return None
        return int(chat_id_s)
    except Exception:
        return None


def session_chat_id(request) -> int | None:
    cookie = request.cookies.get(COOKIE)
    return verify_token(cookie) if cookie else None


# ============================================================ метрики (для админки)

_EVENTS: list[dict] = []          # кольцевой журнал запросов к Авито
_METRIC_TS = {"scan_start": None, "card_start": None}


def metric(kind: str, detail: str = "", dur: float | None = None):
    """Событие запроса к Авито: scan/card/compare/throttle. Для админ-панели."""
    ev = {
        "ts": int(time.time()),
        "kind": kind,
        "detail": str(detail)[:90],
        "dur": round(dur, 1) if dur is not None else None,
    }
    _EVENTS.append(ev)
    if len(_EVENTS) > 300:
        del _EVENTS[:150]
    if kind == "scan_throttle":
        _METRIC_TS["last_throttle"] = ev["ts"]


def is_admin(request) -> bool:
    return request["chat_id"] in set(appconfig.cfg("allowed_chat_ids", []))


def require_auth(fn):
    async def wrapper(request):
        chat_id = session_chat_id(request)
        if chat_id is None:
            return web.Response(status=401, text=json.dumps({"error": "unauthorized"}), content_type="application/json")
        request["chat_id"] = chat_id
        request["is_owner"] = chat_id in set(appconfig.cfg("allowed_chat_ids", []))
        return await fn(request)
    return wrapper


def _json(data) -> web.Response:
    return web.Response(text=json.dumps(data, ensure_ascii=False), content_type="application/json")


def _auth_cookie(resp: web.Response, chat_id: int, request) -> web.Response:
    secure = request.scheme == "https" or bool(appconfig.cfg("web_domain", ""))
    kwargs = dict(httponly=True, max_age=60 * 60 * 24 * 60)
    if secure:
        kwargs.update(secure=True, samesite="None")
    resp.set_cookie(COOKIE, make_token(chat_id), **kwargs)
    return resp


# ============================================================ auth

def _validate_tg_init_data(init_data: str) -> dict | None:
    """Проверка подписи initData Telegram WebApp на токене бота."""
    if not init_data:
        return None
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        hash_in = parsed.pop("hash", None)
        if not hash_in:
            return None
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret = hmac.new(b"WebAppData", str(appconfig.cfg("telegram_bot_token", "")).encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, hash_in):
            return None
        return json.loads(parsed.get("user", "{}"))
    except Exception:
        return None


async def auth_tg(request):
    body = await request.json()
    user = _validate_tg_init_data(body.get("initData", ""))
    if not user or not user.get("id"):
        return _json({"ok": False, "error": "bad initData"})
    chat_id = int(user["id"])
    if not db.is_allowed(chat_id):
        import bot as bot_mod
        username = f"@{user.get('username')}" if user.get("username") else ""
        if getattr(bot_mod, "BOT", None):
            await bot_mod.request_access_now(
                bot_mod.BOT, chat_id, username,
                user.get("first_name", ""), user.get("last_name", ""))
        return _json({"ok": True, "allowed": False})
    return _auth_cookie(_json({"ok": True, "allowed": True, "chat_id": chat_id}), chat_id, request)


async def auth_code(request):
    body = await request.json()
    chat_id = db.consume_auth_code(body.get("code", ""))
    if chat_id is None or not db.is_allowed(chat_id):
        return _json({"ok": False, "error": "Код недействителен или истёк"})
    return _auth_cookie(_json({"ok": True, "chat_id": chat_id}), chat_id, request)


@require_auth
async def me(request):
    users = db.list_allowed_users()
    name = next((u["name"] for u in users if u["chat_id"] == request["chat_id"]), "")
    return _json({"chat_id": request["chat_id"], "is_owner": bool(request["is_owner"]), "name": name})


# ============================================================ поиски

@require_auth
async def api_searches(request):
    return _json(db.list_searches_web(chat_id=request["chat_id"]))


@require_auth
async def api_search_get(request):
    s = db.get_search_for_user(int(request.match_info["sid"]), request["chat_id"])
    if not s:
        return _json({"error": "not found"}, status=404)
    return _json(s)


@require_auth
async def api_search_create(request):
    body = await request.json()
    text = (body.get("text") or "").strip()
    if len(text) < 3:
        return _json({"ok": False, "error": "Опиши, что искать (минимум 3 символа)"})
    if searchflow.has_running_dialog(request["chat_id"]):
        return _json({"ok": False, "error": "Один поиск уже уточняется — дождись его запуска"})
    sid = db.create_search(request["chat_id"], "Запрос принят…", text, {"raw": text}, [])
    db.set_search_status(sid, "PLANNING")
    default_cities = appconfig.cfg("default_city_slugs", ["novokuznetsk"])
    asyncio.create_task(searchflow.plan_search(sid, text, default_cities))
    return _json({"ok": True, "sid": sid})


@require_auth
async def api_search_answer(request):
    sid = int(request.match_info["sid"])
    body = await request.json()
    idx = int(body.get("idx", 0))
    answer = (body.get("answer") or "").strip()
    s = db.get_search_for_user(sid, request["chat_id"])
    if not s or s["status"] != "ASKING":
        return _json({"ok": False, "error": "Поиск не в режиме уточнения"})
    next_q = searchflow.record_answer(sid, idx, answer)
    if next_q is None:
        await searchflow.finalize_search(sid)
        return _json({"ok": True, "done": True})
    return _json({"ok": True, "done": False, "next": next_q, "idx": searchflow.next_question_index(sid)})


@require_auth
async def api_search_skip(request):
    sid = int(request.match_info["sid"])
    s = db.get_search_for_user(sid, request["chat_id"])
    if not s or s["status"] != "ASKING":
        return _json({"ok": False, "error": "нельзя"})
    await searchflow.finalize_search(sid)
    return _json({"ok": True, "done": True})


@require_auth
async def api_search_action(request):
    sid = int(request.match_info["sid"])
    act = request.match_info["action"]
    s = db.get_search_for_user(sid, request["chat_id"])
    if not s:
        return _json({"ok": False, "error": "у тебя нет такого поиска"}, status=404)
    if act == "pause":
        db.set_search_status(sid, "PAUSED")
    elif act == "resume":
        db.set_search_status(sid, "ACTIVE")
        searchflow._wake()
    elif act == "stop":
        db.set_search_status(sid, "STOPPED")
    elif act == "replan" and s["status"] == "PLANNING_FAILED":
        db.set_search_status(sid, "PLANNING")
        default_cities = appconfig.cfg("default_city_slugs", ["novokuznetsk"])
        asyncio.create_task(searchflow.plan_search(sid, s["raw_request"], default_cities))
    elif act == "delete":
        db.delete_search(sid)
    else:
        return _json({"ok": False, "error": "unknown action"}, status=400)
    return _json({"ok": True})


# ============================================================ лоты

@require_auth
async def api_items(request):
    mode = request.query.get("mode", "all")
    search_id = request.query.get("search_id")
    search_id = int(search_id) if search_id and search_id.isdigit() else None
    q = (request.query.get("q") or "").strip()
    return _json(db.list_items_web(mode, chat_id=request["chat_id"], search_id=search_id, q=q))


@require_auth
async def api_item_card(request):
    """Полная карточка лота: с кэшем карточки Авито (описание, характеристики, фото)."""
    sid = int(request.match_info["sid"])
    if not db.get_search_for_user(sid, request["chat_id"]):
        return _json({"error": "not found"}, status=404)
    item = db.get_item_full(sid, request.match_info["iid"])
    if not item:
        return _json({"error": "not found"}, status=404)
    card = db.get_item_card(sid, request.match_info["iid"])
    pos = db.queue_position(sid, request.match_info["iid"])
    return _json({"item": item, "card": card, "queue_position": pos})


@require_auth
async def api_item_card_request(request):
    """Поставить карточку лота в очередь на снятие ботом."""
    sid = int(request.match_info["sid"])
    iid = request.match_info["iid"]
    s = db.get_search_for_user(sid, request["chat_id"])
    if not s:
        return _json({"ok": False, "error": "not found"}, status=404)
    state = db.queue_card_request(sid, iid, request["chat_id"])
    if state == "has_card":
        return _json({"ok": True, "state": "has_card"})
    return _json({"ok": True, "state": state, "position": db.queue_position(sid, iid)})


# ============================================================ админка

@require_auth
async def api_admin(request):
    """Сводка для админа: все поиски, очередь карточек, журнал запросов к Авито."""
    if not is_admin(request):
        return _json({"error": "forbidden"}, status=403)
    searches = db.list_searches_web()
    users = {u["chat_id"]: u.get("name") or "" for u in db.list_allowed_users()}
    for s in searches:
        s["owner"] = users.get(s.get("chat_id"), "")
    now = int(time.time())
    queue = db.list_card_queue_web()
    return _json({
        "now": now,
        "searches": searches,
        "queue": queue,
        "events": list(reversed(_EVENTS[-120:])),
        "cfg": {
            "interval": appconfig.cfg("check_interval_seconds", 120),
            "requests_per_cycle": appconfig.cfg("scan_requests_per_cycle", 1),
            "card_cooldown": appconfig.cfg("card_fetch_cooldown_seconds", 180),
            "proxy": appconfig.cfg("proxy_url", ""),
        },
    })


@require_auth
async def api_admin_search_action(request):
    """Админ управляет чужими поисками: pause/resume/stop/delete."""
    if not is_admin(request):
        return _json({"ok": False, "error": "forbidden"}, status=403)
    sid = int(request.match_info["sid"])
    act = request.match_info["action"]
    if not db.get_search(sid):
        return _json({"ok": False, "error": "нет такого"}, status=404)
    if act == "pause":
        db.set_search_status(sid, "PAUSED")
    elif act == "resume":
        db.set_search_status(sid, "ACTIVE")
    elif act == "stop":
        db.set_search_status(sid, "STOPPED")
    elif act == "delete":
        db.delete_search(sid)
    else:
        return _json({"ok": False, "error": "unknown action"})
    return _json({"ok": True})


@require_auth
async def api_admin_queue_clear(request):
    """Очистить_DONE из очереди карточек (зачистка застрявших PROCESSING)."""
    if not is_admin(request):
        return _json({"ok": False, "error": "forbidden"}, status=403)
    import sqlite3
    conn = sqlite3.connect(db.DB_PATH)
    conn.execute("UPDATE card_queue SET status='QUEUED' WHERE status='PROCESSING'")
    conn.commit()
    conn.close()
    return _json({"ok": True})


# ---------- воркер очереди карточек ----------

async def _card_worker():
    """Фон: снимает карточки из очереди (Аккуратно к IP — между заходами пауза)."""
    cooldown = int(appconfig.cfg("card_fetch_cooldown_seconds", 180))
    while True:
        try:
            batch = db.pop_card_requests(1)
        except Exception:
            log.exception("card queue read failed")
            batch = []
        if not batch:
            await asyncio.sleep(15)
            continue
        r = batch[0]
        sid, iid, url = r["search_id"], r["avito_id"], r["url"]
        log.info("🃏 Воркер: снимаю карточку %s (поиск %s)", iid, sid)
        db.set_card_request_status(sid, iid, "PROCESSING")
        _METRIC_TS["card_start"] = time.time()
        try:
            import avito
            pw, browser, page = await asyncio.wait_for(avito.open_browser_context(), timeout=40)
            try:
                info = await asyncio.wait_for(avito.inspect_item(page, url), timeout=150)
            finally:
                try:
                    await asyncio.wait_for(avito.close_browser_context(pw, browser), timeout=20)
                except Exception:
                    log.exception("воркер: браузер не закрылся чисто")
            if info.get("description") or info.get("params") or info.get("photos"):
                db.save_item_card(sid, iid, info)
                db.set_card_request_status(sid, iid, "DONE")
                metric("card", f"✅ {iid}", time.time() - _METRIC_TS["card_start"])
                log.info("🃏 карточка %s снята (%s фото)", iid, len(info.get("photos") or []))
            else:
                # Авито, скорее всего, показал заглушку — вернём в очередь
                db.set_card_request_status(sid, iid, "QUEUED")
                metric("card", f"⛔ {iid} (блок/пусто)", time.time() - _METRIC_TS["card_start"])
                log.info("🃏 карточка %s не открылась — вернул в очередь", iid)
                await asyncio.sleep(max(cooldown * 2, 300))
                continue
        except Exception:
            log.exception("card worker failed for %s", iid)
            db.set_card_request_status(sid, iid, "QUEUED")
        await asyncio.sleep(cooldown)


@require_auth
async def api_flag(request):
    sid = int(request.match_info["sid"])
    if not db.get_search_for_user(sid, request["chat_id"]):
        return _json({"ok": False}, status=404)
    val = db.toggle_item_flag(sid, request.match_info["iid"], request.match_info["field"])
    return _json({"ok": val is not None, "value": val})


# ============================================================ сравнение

_COMPARES: dict[int, dict] = {}   # chat_id -> {"state": ..., "items": [...], "result": ...


async def _do_compare(chat_id: int, rows: list, note: str):
    state = _COMPARES.setdefault(chat_id, {"state": "processing"})
    state["state"] = "processing"
    try:
        import avito
        import ai as ai_client
        cards = []
        # сначала берём карточки из кэша; заходим на Авито только к тем, которых нет
        missing = []
        for r in rows:
            cached = db.get_item_card(r["search_id"], r["avito_id"])
            if cached is not None:
                cards.append({
                    "title": r["title"],
                    "price": r["price"],
                    "params": cached.get("params") or [],
                    "description": cached.get("description") or r["snippet"] or "",
                })
            else:
                missing.append(r)

        if missing:
            pw, browser, page = await avito.open_browser_context()
            try:
                for r in missing:
                    info = await avito.inspect_item(page, r["url"])
                    if info.get("is_reserved"):
                        info = {"description": "", "params": [], "photos": []}
                    elif info.get("defect"):
                        d = info["defect"]
                        info = {"description": f"(Возможный дефект: {d})", "params": [], "photos": []}
                    # сохраняем в кэш, чтобы в следующий раз и другим вайм
                    db.save_item_card(r["search_id"], r["avito_id"], info)
                    cards.append({
                        "title": r["title"],
                        "price": r["price"],
                        "params": info.get("params") or [],
                        "description": info.get("description") or r["snippet"] or "",
                    })
            finally:
                try:
                    await asyncio.wait_for(avito.close_browser_context(pw, browser), timeout=20)
                except Exception:
                    log.exception("браузер сравнения не закрылся чисто")

        verdict = await ai_client.compare_items(cards, note)
        if not verdict:
            state.update(state="error", error="Нейросеть не вернула ответ — попробуй ещё раз чуть позже")
            return
        state.update(state="done", result=verdict, ts=int(time.time()))
    except Exception as e:
        log.exception("compare failed")
        state.update(state="error", error=f"Не удалось: {e}")


@require_auth
async def api_compare_start(request):
    chat_id = request["chat_id"]
    body = await request.json()
    keys = list(body.get("items") or [])[:6]
    note = (body.get("note") or "").strip()[:500]
    if len(keys) < 2:
        return _json({"ok": False, "error": "Добавь минимум 2 товара в сравнение"})
    rows = []
    for k in keys:
        try:
            sid_s, iid = str(k).split(":", 1)
            sid = int(sid_s)
        except Exception:
            continue
        s = db.get_search_for_user(sid, chat_id)
        if not s:
            continue
        item = db.get_item_full(sid, iid)
        if item and item["url"]:
            rows.append(item)
    if len(rows) < 2:
        return _json({"ok": False, "error": "Не нашёл выбранные лоты"})
    cur = _COMPARES.get(chat_id)
    if cur and cur.get("state") == "processing":
        return _json({"ok": False, "error": "Сравнение уже идёт — подожди"})
    _COMPARES[chat_id] = {"state": "processing", "items": [
        {"key": k, "title": r["title"], "price": r["price"]} for k, r in zip(keys, rows)], "result": None}
    asyncio.create_task(_do_compare(chat_id, rows, note))
    return _json({"ok": True, "state": "processing"})


@require_auth
async def api_compare_status(request):
    cmp = _COMPARES.get(request["chat_id"]) or {"state": "idle"}
    return _json(cmp)


# ============================================================ страница

PAGE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0a0d12">
<title>🛒 Avito Шопер</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{
  --bg:#0a0d12; --panel:#11161d; --panel2:#171e28; --panel3:#1c2430;
  --line:#232b38; --line2:#2e3949;
  --text:#eaf0f6; --muted:#8a96a8;
  --accent:#34d399; --accent2:#22d3ee; --violet:#a78bfa; --warn:#fbbf24; --bad:#f87171;
  --grad:linear-gradient(135deg,#34d399,#22d3ee);
  --gradc:linear-gradient(135deg,#a78bfa,#7c5cff);
  --r:16px;
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,system-ui,'Segoe UI',Roboto,Arial,sans-serif;-webkit-font-smoothing:antialiased}
body{min-height:100vh;padding-bottom:calc(var(--nav-h) + env(safe-area-inset-bottom) + 8px)}

/* ---------- header ---------- */
header{position:sticky;top:0;z-index:10;display:flex;align-items:center;gap:10px;
  padding:12px 16px;background:rgba(10,13,18,.86);backdrop-filter:blur(14px);
  border-bottom:1px solid var(--line)}
.logo{width:34px;height:34px;border-radius:11px;background:var(--grad);display:flex;align-items:center;justify-content:center;font-size:18px;box-shadow:0 2px 12px rgba(52,211,153,.35)}
h1{font-size:17px;margin:0;font-weight:800;letter-spacing:.2px}
h1 small{display:block;font-size:10.5px;color:var(--muted);font-weight:600;letter-spacing:1.4px;text-transform:uppercase}
.who{margin-left:auto;font-size:11.5px;color:var(--muted);text-align:right}

/* ---------- bottom nav ---------- */
nav{--nav-h:64px;position:fixed;z-index:20;left:0;right:0;bottom:0;display:flex;
  justify-content:space-around;align-items:stretch;height:calc(var(--nav-h) + env(safe-area-inset-bottom));
  padding-bottom:env(safe-area-inset-bottom);
  background:rgba(13,17,24,.92);backdrop-filter:blur(16px);border-top:1px solid var(--line)}
nav .tab{flex:1;max-width:130px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;
  color:var(--muted);font-size:10.5px;font-weight:700;cursor:pointer;position:relative;transition:color .15s}
nav .tab .ic{font-size:21px;line-height:1;filter:grayscale(1) opacity(.75);transition:.15s}
nav .tab.on{color:var(--accent)}
nav .tab.on .ic{filter:none;transform:translateY(-2px)}
nav .tab.on::before{content:'';position:absolute;top:0;width:26px;height:3px;border-radius:0 0 3px 3px;background:var(--grad)}
nav .tab .nub{position:absolute;top:6px;right:calc(50% - 20px);min-width:16px;height:16px;border-radius:9px;
  background:var(--gradc);color:#fff;font-size:9.5px;font-weight:800;display:none;align-items:center;justify-content:center;padding:0 4px}
nav .tab.sel .nub{display:flex}

/* ---------- layout ---------- */
.wrap{max-width:980px;margin:0 auto;padding:14px 14px 10px}
viewtabs{display:none}
.card{background:linear-gradient(180deg,var(--panel2),var(--panel));border:1px solid var(--line);
  border-radius:var(--r);padding:14px;margin:10px 0;transition:border-color .15s}
.card:hover{border-color:var(--line2)}
.row{display:flex;gap:12px;align-items:flex-start}
.grow{flex:1;min-width:0}
.muted{color:var(--muted);font-size:12px}
.title4{font-size:16.5px;font-weight:800}
section h3{margin:2px 0 0;font-size:16px;font-weight:800}

/* ---------- chips ---------- */
.chips{display:flex;gap:7px;overflow-x:auto;padding:10px 14px;scrollbar-width:none;
  position:sticky;top:58px;z-index:8;background:linear-gradient(180deg,var(--bg) 70%,transparent)}
.chips::-webkit-scrollbar{display:none}
.chip{flex:0 0 auto;padding:7px 14px;border:1px solid var(--line);border-radius:20px;background:var(--panel2);
  color:var(--muted);cursor:pointer;font-size:12.5px;font-weight:650;transition:.15s;white-space:nowrap}
.chip.on{background:var(--accent);border-color:var(--accent);color:#05231a;font-weight:750}

/* ---------- item card ---------- */
.item-card{position:relative}
.acts{position:absolute;top:12px;right:12px;display:flex;gap:6px;flex-shrink:0;z-index:2}
.act-under{position:absolute;top:12px;right:106px;font-size:11px;color:var(--muted);z-index:1}
.pricing{font-weight:800;font-size:18px;color:var(--accent);letter-spacing:.2px}
.badge{display:inline-block;padding:2.5px 9px;border-radius:10px;font-size:10.5px;font-weight:750;color:#0a0d12;vertical-align:1px}
.b-match{background:var(--warn)}/* совпадение */ .b-match{background:#a9f0cd}
.b-close{background:var(--warn)}
.b-reject{background:var(--bad);color:#fff}
.b-nodata{background:#3a4454;color:#c6cfe0} .b-defect{background:#3a4454;color:#c6cfe0}
.b-active{background:var(--accent2)} .b-paused{background:var(--warn)} .b-stopped{background:var(--bad);color:#fff}
.b-asking{background:#8ab4ff} .b-planning{background:var(--violet)}
.acts{display:flex;gap:6px;flex-shrink:0}
.it-btn{width:40px;height:40px;border:1px solid var(--line);background:var(--panel3);border-radius:12px;
  cursor:pointer;font-size:17px;color:var(--text);transition:.15s;line-height:1}
.it-btn:hover{border-color:var(--line2);transform:translateY(-1px)}
.it-btn.on{background:linear-gradient(135deg,rgba(251,191,36,.25),rgba(251,191,36,.1));border-color:var(--warn)}
.it-btn.cmp.on{background:linear-gradient(135deg,rgba(167,139,250,.28),rgba(124,92,255,.12));border-color:var(--violet)}
.it-btn.danger:hover{border-color:var(--bad)}
a.lnk{color:var(--accent2);text-decoration:none;font-size:12.5px;font-weight:650}
.hidden-item{opacity:.45;filter:saturate(.4)}

.this-card{background:var(--panel2);border:1px solid var(--line);border-radius:14px;margin:10px 0}
.act-btn{flex:1;display:flex;align-items:center;justify-content:center;gap:6px;border:1px solid var(--line);background:var(--panel3);color:var(--text);border-radius:10px;padding:9px 12px;cursor:pointer;font-size:13px;font-weight:700;transition:.13s;text-align:center}
.act-btn:hover{border-color:var(--line2)}
.act-btn.violet{background:linear-gradient(135deg,rgba(167,139,250,.25),rgba(124,92,255,.12));border-color:var(--violet)}
.act-btn.warn{background:linear-gradient(135deg,rgba(251,191,36,.22),rgba(251,191,36,.08));border-color:var(--warn)}
.act-btn.danger:hover{border-color:var(--bad);color:var(--bad)}

/* ---------- поиск и фильтры ---------- */
.searchbox{display:flex;gap:8px;align-items:center;margin:2px 0 6px}
.searchbox input{flex:1;border-radius:22px;padding:10px 15px;font-size:14px}
.searchbox .fb{flex:0 0 auto;border:1px solid var(--line);background:var(--panel2);color:var(--text);border-radius:22px;padding:10px 13px;font-size:13px;cursor:pointer;white-space:nowrap}
.searchbox .fb.has{border-color:var(--violet);color:var(--violet)}

/* ---------- модалки ---------- */
.modal{position:fixed;inset:0;z-index:60;background:rgba(4,7,12,.72);backdrop-filter:blur(3px);display:none;align-items:flex-end;justify-content:center}
.modal.open{display:flex}
.sheet{background:var(--panel);border:1px solid var(--line);border-radius:18px 18px 0 0;width:100%;max-height:88vh;overflow-y:auto;padding:14px 15px calc(env(safe-area-inset-bottom) + 14px)}
.sheet.grab{width:42px;height:4px;border-radius:3px;background:var(--line2);margin:-4px auto 10px}
.modal .sheet h3{display:flex;align-items:center;gap:8px}
.sheet .x{position:sticky;top:0;float:right;width:32px;height:32px;border-radius:50%;background:var(--panel3);border:1px solid var(--line);color:var(--muted);cursor:pointer;z-index:2}
@media(min-width:700px){
  .modal{align-items:center}
  .sheet{max-width:640px;border-radius:18px}
}

/* карточка товара в модалке */
.gal{display:flex;gap:8px;overflow-x:auto;scroll-snap-type:x mandatory;scrollbar-width:none;margin:10px 0 4px;padding-bottom:2px}
.gal::-webkit-scrollbar{display:none}
.gal img{height:150px;flex:0 0 auto;border-radius:12px;scroll-snap-align:center;border:1px solid var(--line);object-fit:cover;background:var(--panel3)}
.param-list{margin:8px 0 0;padding:0;list-style:none}
.param-list li{padding:5px 0;border-bottom:1px solid var(--line);font-size:13px;color:#cbd5e1}
.param-list li:last-child{border:none}
.desc{white-space:pre-wrap;font-size:13.5px;line-height:1.65;color:#dbe3ec;margin-top:6px}
.big-price{font-size:22px;font-weight:800;color:var(--accent)}

/* ---------- пустые / загрузка ---------- */
.ghosting{text-align:center;color:var(--muted);padding:50px 16px;font-size:14.5px;line-height:1.6}
.ghosting b{display:block;font-size:34px;margin-bottom:8px;filter:grayscale(.15)}
.spinner{display:inline-block;width:15px;height:15px;border:2px solid #6b7488;border-top-color:var(--accent);border-radius:50%;animation:sp .7s linear infinite;vertical-align:-3px}
@keyframes sp{to{transform:rotate(360deg)}}
.skel{height:86px;border-radius:var(--r);background:var(--panel);border:1px solid var(--line);margin:10px 0;position:relative;overflow:hidden}
.skel::after{content:'';position:absolute;inset:0;background:linear-gradient(90deg,transparent,rgba(255,255,255,.05),transparent);animation:shimmer 1.5s infinite}
@keyframes shimmer{to{transform:translateX(100%)}}

/* ---------- формы ---------- */
textarea,input{width:100%;background:var(--panel3);color:var(--text);border:1px solid var(--line);
  border-radius:12px;padding:11px 13px;font:inherit;font-size:14.5px;resize:vertical}
textarea{min-height:96px}
textarea:focus,input:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px rgba(52,211,153,.12)}
.btn{border:none;border-radius:12px;padding:12px 20px;font-size:14px;font-weight:800;cursor:pointer;transition:.15s;color:#05231a}
.btn.primary{background:var(--grad)}
.btn.violet{background:var(--gradc);color:#fff}
.btn:disabled{opacity:.45;cursor:not-allowed;filter:grayscale(.4)}
.btn.ghost{background:var(--panel3);border:1px solid var(--line);color:var(--text)}
.must{margin:8px 0 0;padding-left:18px;font-size:12.5px;color:#c4cbd8;line-height:1.55}
.must .no{color:#f19994}

/* ---------- compare ---------- */
.pick{display:flex;align-items:center;gap:10px;background:var(--panel2);border:1px solid var(--line);
  border-radius:12px;padding:9px 12px;margin:7px 0}
.pick.rm{margin-left:auto;background:none;border:none;color:var(--bad);font-size:16px;cursor:pointer}
.phone-note{margin:10px 0 0}
.cmp-result .winner{border:1.5px solid transparent;background:linear-gradient(var(--panel2),var(--panel)) padding-box,var(--gradc);border-radius:var(--r);padding:14px;margin:10px 0}
.cmp-result .winner h4{margin:0 0 6px;font-size:15.5px;color:var(--violet)}
.cmp-result .why{color:#cfe8de;font-size:13.5px;line-height:1.6;white-space:pre-wrap}
.cmp-result details{margin:8px 0;border:1px solid var(--line);border-radius:12px;padding:10px 13px;background:var(--panel)}
.cmp-result summary{cursor:pointer;font-weight:700;font-size:13px;color:var(--text)}
.cmp-result .tips{margin-top:10px;font-size:12.5px;background:rgba(251,191,36,.08);border:1px solid rgba(251,191,36,.25);color:#f3dcc0;border-radius:12px;padding:10px 13px;line-height:1.55}

/* ---------- toast ---------- */
.toast{position:fixed;bottom:calc(var(--nav-h) + env(safe-area-inset-bottom) + 12px + var(--x,0px));left:50%;transform:translateX(-50%) translateY(8px);
  background:#122b22;color:#b9f5dc;border:1px solid rgba(52,211,153,.4);padding:11px 20px;border-radius:13px;
  font-weight:700;font-size:13px;opacity:0;transition:.25s;z-index:30;pointer-events:none;max-width:92vw;text-align:center}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
.toast.err{background:#2b1214;color:#ffd2d0;border-color:rgba(248,113,113,.45)}

/* login */
#login{max-width:380px;margin:70px auto}
@media(min-width:900px){
  body{padding-bottom:20px}
  nav{--nav-h:auto;position:static;height:auto;padding:0;background:none;border:none;justify-content:flex-start;gap:6px;margin-left:10px}
  nav .tab{flex:none;flex-direction:row;gap:7px;font-size:13px;padding:9px 16px;border:1px solid var(--line);border-radius:24px;background:var(--panel2);color:var(--muted)}
  nav .tab.on{border-color:transparent;color:#05231a;background:var(--grad)}
  nav .tab.on .ic{transform:none}
  nav .tab .nub{position:static;display:flex;min-width:17px;height:17px;margin-top:-4px}
  nav .tab.sel:not(.on) .nub{display:flex}
  .chips{position:sticky;top:64px;padding:10px 0}
}
</style>
</head>
<body>
<header>
  <div class="logo">🛒</div>
  <h1>Avito Шопер<small>поиск под тебя</small></h1>
  <div class="who" id="who"></div>
  <nav id="nav">
    <div class="tab on" data-tab="items" onclick="setTab('items')"><span class="ic">📦</span>Лоты</div>
    <div class="tab" data-tab="searches" onclick="setTab('searches')"><span class="ic">🔍</span>Поиски</div>
    <div class="tab" data-tab="cmp" onclick="setTab('cmp')"><span class="ic">⚖️</span>Сравнение<span class="nub" id="cmp-nub">0</span></div>
    <div class="tab" data-tab="new" onclick="setTab('new')"><span class="ic">➕</span>Новый</div>
    <div class="tab" data-tab="admin" onclick="setTab('admin')" style="display:none"><span class="ic">🛠</span>Админ</div>
  </nav>
</header>

<div id="login" class="wrap">
  <div class="card">
    <h3>Вход</h3>
    <p class="muted">Если открыл сайт из бота ( кнопка «🌐 Веб-приложение» ) — вход произойдёт сам.
    Иначе введи код, который выдаст бот командой <b>/link</b>.</p>
    <input id="code" placeholder="6-значный код" inputmode="numeric" maxlength="6">
    <div style="margin-top:10px"><button class="btn primary" onclick="loginByCode()">Войти</button></div>
    <p id="login-err" class="muted" style="color:var(--bad)"></p>
  </div>
</div>

<div id="app" class="wrap" style="display:none">
  <div id="v-items">
    <div class="chips" id="mode-chips">
      <button class="chip on" onclick="setMode(this,'notified')">Показанные</button>
      <button class="chip" onclick="setMode(this,'favorites')">⭐ Избранное</button>
      <button class="chip" onclick="setMode(this,'hidden')">👻 Скрытые</button>
      <button class="chip" onclick="setMode(this,'rejected')">Отброшенные</button>
      <button class="chip" onclick="setMode(this,'all')">Все</button>
    </div>
    <div class="searchbox">
      <input id="q-search" placeholder="🔎 Поиск по лотам (название, описание…)" oninput="onSearchInput()">
      <button class="fb" id="fb-btn" onclick="openFilterModal()">🎛 Поиски</button>
    </div>
    <div class="muted" id="q-hint" style="display:none;margin:0 4px 6px"></div>
    <div id="items-list"></div>
  </div>

  <div id="v-searches" style="display:none"><div id="searches-list"></div></div>

  <div id="v-cmp" style="display:none">
    <div id="cmp-basket"></div>
    <div class="card" id="cmp-controls" style="display:none">
      <p class="muted phone-note" style="margin:0 0 8px">Нейросеть зайдёт на Авито и сравнит карточки целиком.
        Можно уточнить, что для тебя важнее:</p>
      <input id="cmp-note" placeholder="например: важен вес и батарея, а не камера">
      <div style="margin-top:10px;display:flex;gap:8px">
        <button class="btn violet" id="cmp-go" onclick="startCompare()">⚖️ Сравнить</button>
        <button class="btn ghost" onclick="clearCmp()">Очистить</button>
      </div>
      <div id="cmp-box"></div>
    </div>
    <div class="ghosting" id="cmp-empty">⚖️ Отметь 2–6 лотов кнопкой ⚖️ в списке лотов — и сравни их здесь.</div>
  </div>

  <div id="v-new" style="display:none">
    <div class="card">
      <h3>Новый поиск</h3>
      <p class="muted">Опиши желаемое обычным текстом, например: <i>«найди ноутбук 16/512, бренды asus и redmi,
      Ryzen но не U-серии, экран 13–15.6, от 40 до 70 тысяч»</i></p>
      <textarea id="new-text" placeholder="ноутбук 16/512, asus/redmi, Ryzen без U, экран 13-15.6, 40-70к"></textarea>
      <div style="margin-top:10px"><button class="btn primary" id="new-btn" onclick="createSearch()">🧠 Собрать план поиска</button></div>
      <div id="new-status"></div>
    </div>
  </div>

  <div id="v-admin" style="display:none">
    <div class="card" id="adr-tempo"></div>
    <div class="card" id="adr-queue"></div>
    <div class="card" id="adr-events"></div>
    <div class="card" id="adr-searches"></div>
  </div>
</div>

<div id="toast" class="toast"></div>

<div class="modal" id="modal" onclick="modalBg(event)">
  <div class="sheet">
    <div class="grab"></div>
    <div id="modal-body"></div>
  </div>
</div>

<script>
const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
if (tg) { tg.ready(); tg.expand(); }

// ---------------- поиск и фильтры лотов ----------------
let state = { tab:'items', mode:'notified', searchFilter:null, sid:null, searches:[], pollItems:null, pollNew:null, cmp:[], curQ:null, q:'', qTimer:null };

function esc(s){ return (s??'').toString().replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;'); }
function rub(v){ return v ? Number(v).toLocaleString('ru-RU')+' ₽' : '—'; }
function toast(msg, err){
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.toggle('err', !!err); t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'), 3200);
}
async function api(path, body, method){
  const opt = {method: method || (body?'POST':'GET'), headers:{'Content-Type':'application/json'}};
  if (body) opt.body = JSON.stringify(body);
  const r = await fetch(path, opt);
  if (r.status === 401) { logoutUI(); throw new Error('unauthorized'); }
  return r.json();
}
function stopPollers(){
  if (state.pollItems) { clearInterval(state.pollItems); state.pollItems = null; }
  if (state.pollNew) { clearInterval(state.pollNew); state.pollNew = null; }
  if (state.pollAdmin) { clearInterval(state.pollAdmin); state.pollAdmin = null; }
}
function setTab(t){
  state.tab = t; stopPollers();
  ['items','searches','cmp','new','admin'].forEach(x=>{
    document.getElementById('v-'+x).style.display = x===t?'block':'none';
  });
  document.querySelectorAll('#nav .tab').forEach(el=>el.classList.toggle('on', el.dataset.tab===t));
  if (t==='items'){ loadItems(); state.pollItems = setInterval(loadItems, 8000); }
  if (t==='searches') loadSearches();
  if (t==='cmp'){ renderCmp(); pollCompare(); }
  if (t==='new') document.getElementById('new-text').focus();
  if (t==='admin'){ loadAdmin(); state.pollAdmin = setInterval(loadAdmin, 5000); }
  window.scrollTo(0,0);
}

// ---------------- auth ----------------
async function loginByCode(){
  const code = document.getElementById('code').value.trim();
  if (code.length !== 6) { document.getElementById('login-err').textContent = 'Код из 6 цифр'; return; }
  const r = await api('/api/auth/code', {code});
  if (!r.ok) { document.getElementById('login-err').textContent = r.error || 'Не удалось'; return; }
  startApp();
}
function logoutUI(){
  document.getElementById('app').style.display='none';
  document.getElementById('login').style.display='block';
}

// ---------------- boot ----------------
async function startBoot(){
  if (tg && tg.initData) {
    try {
      const r = await api('/api/auth/tg', {initData: tg.initData});
      if (!r.ok) { toast('Не удалось проверить подпись', true); return; }
      if (r.allowed === false) { document.getElementById('login').innerHTML =
        '<div class="card"><h3>⏳ Заявка отправлена</h3><p class="muted">Жди одобрения от владельца бота. Тогда в боте /link тоже сработает.</p></div>'; return; }
    } catch(e) {}
  }
  try { await startApp(); } catch(e) { /* нет сессии — остаётся форма входа */ }
}
async function startApp(){
  let me;
  try { me = await api('/api/me'); } catch(e) { logoutUI(); return; }
  document.getElementById('login').style.display='none';
  document.getElementById('app').style.display='block';
  document.getElementById('who').textContent = (me.name || '') + ' · ' + me.chat_id;
  const adm = document.querySelector('#nav .tab[data-tab="admin"]');
  if (adm) adm.style.display = me.is_owner ? '' : 'none';
  loadChips();
  setTab('items');
}

// ---------------- чипсы: фильтры по поискам (в модалке) ----------------
async function loadChips(){
  state.searches = await api('/api/searches');
  const active = state.searches.filter(s=>s.items_count > 0);
  document.getElementById('fb-btn').classList.toggle('has', !!state.searchFilter);
  updQHint(active);
}
function updQHint(active){
  // полоска-подсказка под поиском: какой фильтр включён
  const hint = document.getElementById('q-hint');
  const f = (state.searches||[]).find(s=>s.id===state.searchFilter);
  if (state.searchFilter && f){
    hint.style.display = 'block';
    hint.innerHTML = `Только «${esc(f.title)}» · <a href="#" onclick="resetFilter(event)">сбросить</a>`;
  } else {
    hint.style.display = 'none';
  }
}
function resetFilter(ev){
  if (ev) ev.preventDefault();
  state.searchFilter = null;
  document.getElementById('fb-btn').classList.remove('has');
  updQHint(); loadItems();
}
function onSearchInput(){
  clearTimeout(state.qTimer);
  state.qTimer = setTimeout(()=>{
    const was = state.q;
    state.q = document.getElementById('q-search').value.trim();
    if (was !== state.q) loadItems();
  }, 350);
}
function openFilterModal(){
  const opts = (state.searches||[]).map(s=>`
    <button class="act-btn ${state.searchFilter===s.id?'violet':''}" style="margin:5px 0;width:100%;justify-content:flex-start" onclick="pickSearch(${s.id})">
      №${s.id} ${esc(s.title).slice(0,34)} <span class="muted">· ${s.items_count} лотов</span>
    </button>`).join('') || '<div class="muted">Поисков ещё нет</div>';
  showModal(`
    <h3>🎛 Фильтр по поиску</h3>
    <p class="muted" style="margin:6px 0">Показать лоты, найденные одним поиском — или всё сразу.</p>
    <button class="act-btn ${state.searchFilter===null?'violet':''}" style="width:100%;justify-content:flex-start" onclick="pickSearch(null)">🌐 Все поиски</button>
    ${opts}
  `);
}
function pickSearch(sid){
  state.searchFilter = sid;
  document.getElementById('fb-btn').classList.toggle('has', !!sid);
  closeModal();
  updQHint();
  loadItems();
}
function setMode(el, m){
  state.mode = m;
  document.querySelectorAll('#mode-chips .chip').forEach(c=>c.classList.remove('on'));
  el.classList.add('on');
  loadItems();
}
function setSearchFilter(el, sid){
  state.searchFilter = sid;
  if (el) {
    document.querySelectorAll('#search-chips .chip').forEach(c=>c.classList.remove('on'));
    el.classList.add('on');
  }
  loadItems();
}

// ---------------- модалки ----------------
function showModal(html){
  const m = document.getElementById('modal');
  document.getElementById('modal-body').innerHTML = html;
  m.classList.add('open');
}
function closeModal(){ document.getElementById('modal').classList.remove('open'); }
function modalBg(ev){ if (ev.target === ev.currentTarget) closeModal(); }

// ---------------- карточка товара ----------------
async function openItem(sid, iid){
  let data;
  try { data = await api(`/api/items/${sid}/${iid}`); } catch(e){ return; }
  const it = data.item || {};
  const card = data.card;
  const pos = data.queue_position || 0;
  const params = (card && card.params || []).map(p=>`<li>${esc(p)}</li>`).join('');
  const photos = ((card && card.photos) || []).map(u=>`<img src="${esc(u)}" loading="lazy" onclick="event.stopPropagation()" alt="фото">`).join('');
  showModal(`
    <div class="x" onclick="closeModal()">✕</div>
    <h3>${esc(it.title||'').slice(0,70)}</h3>
    <div style="margin:6px 0 2px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
      <span class="big-price">${rub(it.price)}</span> ${verBadge(it)}
    </div>
    <div class="muted">${esc(it.created_at||'')} · поиск «${esc(it.search_title||'')}»${card&&card.fetched_at?' · карточка снята: '+esc(card.fetched_at):''}</div>
    ${photos ? `<div class="gal">${photos}</div>` : ''}
    ${card && card.description ? `<div style="margin:10px 0 0;font-size:12px;color:var(--muted)">Описание продавца</div><div class="desc">${esc(card.description)}</div>` : ''}
    ${params ? `<div style="margin:12px 0 0;font-size:12px;color:var(--muted)">Характеристики</div><ul class="param-list">${params}</ul>` : ''}
    ${!card ? `<div class="ghosting"><b>🕐</b>${pos
      ? '⏳ Запрошено — в очереди на снятие, позиция ' + pos + '.<br>Карточка появится, когда бот зайдёт на лот.'
      : 'Карточка ещё не снималась. Запроси — бот зайдёт на лот с Авито и достанет фото, описание и характеристики.'}</div>
    <div style="display:flex;justify-content:center;margin-top:8px">
      <button class="act-btn violet" onclick="requestCard('${sid}','${esc(iid)}')">🔎 Запросить</button>
    </div>` : ''}
    <div style="display:flex;gap:8px;margin-top:14px;flex-wrap:wrap">
      <button class="act-btn warn" onclick="closeModal();toggleItemFlagFromModal('${sid}','${esc(iid)}','favorite');">⭐ В избранное</button>
      <button class="act-btn violet" onclick="toggleFromModal('${sid}','${esc(iid)}')">⚖️ В сравнение</button>
      <button class="act-btn danger" onclick="closeModal();toggleItemFlagFromModal('${sid}','${esc(iid)}','hidden');">👁 ${it.hidden?'Вернуть':'Скрыть'}</button>
      <a class="act-btn" href="${esc(it.url||'#')}" target="_blank" rel="noopener" style="text-decoration:none">↗ На Авито</a>
    </div>
  `);
}
async function toggleItemFlagFromModal(sid, iid, field){
  await api(`/api/items/${sid}/${iid}/flag/${field}`, {});
  loadItems();
}
async function requestCard(sid, iid){
  let r;
  try { r = await api(`/api/items/${sid}/${iid}/request`, {}); } catch(e){ return; }
  if (r.state === 'has_card') { toast('✅ Карточка уже снята — закрой и открой лота'); }
  else if (r.state === 'already') { toast('⏳ Уже в очереди — позиция ' + (r.position ?? '?')); }
  else { toast('✅ Поставил в очередь — позиция ' + (r.position ?? '?')); }
  clearInterval(state.pollCard);
  state.pollCard = setInterval(()=>{
    if (!document.getElementById('modal').classList.contains('open')) { clearInterval(state.pollCard); return; }
    openItem(sid, iid);
  }, 10000);
}

// ---------------- админка ----------------
let ADMIN = null;
function ago(ts, now){ const d = now - ts; if (d < 90) return d + ' с назад';
  if (d < 5400) return Math.round(d/60) + ' мин назад';
  if (d < 172800) return Math.round(d/3600) + ' ч назад';
  return Math.round(d/86400) + ' дн назад'; }
function fmtDur(d){ return d ? (d < 100 ? d.toFixed(1) + 'с' : Math.round(d) + 'с') : ''; }
async function loadAdmin(){
  let d;
  try { d = await api('/api/admin'); } catch(e){ return; }
  ADMIN = d;
  // tempo (склад сервера)
  document.getElementById('adr-tempo').innerHTML =
    `<h3>🛠 Ритм бота</h3>
     <p class="muted">интервал: <b>${d.cfg.interval}с</b> · запросов за цикл: <b>${d.cfg.requests_per_cycle}</b> ·
     кулдаун карточек: <b>${d.cfg.card_cooldown}с</b></p>
     <p class="muted" style="font-size:10px">${esc((d.cfg.proxy||'').replace(/^http:\/\/.*?@/, 'прокси: '))}</p>`;
  // queue
  const q = (d.queue||[]).map(x=>
    `<div class="card" style="margin:6px 0;padding:8px 10px">
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <span class="badge b-${x.status==='DONE'?'match':(x.status==='PROCESSING'?'rec':'nodata')}">${x.status}</span>
        <b>${esc((x.title||'').slice(0,42))}</b>
        <span class="muted">${rub(x.price)}</span>
        <span class="muted">· поиск №${x.search_id} · ${ago(x.requested_at, d.now)}</span>
      </div>
    </div>`).join('') || '<p class="muted">Очередь пуста</p>';
  document.getElementById('adr-queue').innerHTML =
    `<h3>🃏 Очередь карточек (запросы с Авито)</h3>${q}`;
  // events
  const ev = (d.events||[]).map(e=>{
    const ic = e.kind==='scan'?'🔄':(e.kind==='scan_throttle'?'⛔':(e.kind==='card'?'🃏':'⚖️'));
    return `<div style="display:flex;gap:8px;padding:3px 0;font-size:12px;align-items:baseline;flex-wrap:wrap">
      <span class="muted">${new Date(e.ts*1000).toLocaleTimeString('ru-RU')}</span>
      <span>${ic}</span><span>${esc(e.detail)}</span>
      ${e.dur?`<span class="muted">· ${fmtDur(e.dur)}</span>`:''}
    </div>`;}).join('') || '<p class="muted">Пока тишина</p>';
  document.getElementById('adr-events').innerHTML =
    `<h3>⏱ Запросы к Авито (живой журнал)</h3>${ev}`;
  // searches
  const st = {ACTIVE:['✅ работает','b-match'], PAUSED:['⏸ на паузе','b-close'],
              STOPPED:['⏹ остановлен','b-defect'], ASKING:['❓ опрашиваю','b-nodata'],
              PLANNING:['🧠 планирую','b-nodata'], PLANNING_FAILED:['❌ ошибка плана','b-defect']};
  const ss = (d.searches||[]).map(s=>{
    const [label, cls] = st[s.status] || [s.status, 'b-nodata'];
    const last = s.last_scan_at ? (d.now*1000 - Date.parse((s.last_scan_at+'Z').replace(' ','T'))) : null;
    return `<div class="card" style="margin:8px 0;padding:10px 12px">
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <span class="badge ${cls}">${label}</span>
        <b>№${s.id}</b><span>${esc((s.title||'').slice(0,40))}</span>
        <span class="muted">· ${esc(s.owner||'')} (${s.chat_id})</span>
      </div>
      <div style="display:flex;gap:10px;margin-top:5px;font-size:12px" class="muted">
        <span>📦 лотов: <b style="color:var(--text)">${s.items_count}</b></span>
        <span>✅ подошло: <b style="color:var(--text)">${s.matched_count}</b></span>
        <span>🕓 ${last ? 'скан ' + ago(Math.round(last/1000), d.now) : 'ещё не сканился'}</span>
      </div>
      <div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap">
        ${s.status==='ACTIVE'?'':`<button class="btn ghost sm" onclick="adminAct(${s.id},'resume')">▶ Старт</button>`}
        ${s.status==='ACTIVE'?`<button class="btn ghost sm" onclick="adminAct(${s.id},'pause')">⏸ Пауза</button>`:''}
        ${s.status!=='STOPPED'?`<button class="btn ghost sm" onclick="adminAct(${s.id},'stop')">⏹ Стоп</button>`:''}
        <button class="btn ghost sm" style="color:var(--bad)" onclick="adminAct(${s.id},'delete')">🗑 Удалить</button>
      </div>
    </div>`;
  }).join('') || '<p class="muted">Поисков нет</p>';
  document.getElementById('adr-searches').innerHTML =
    `<h3>📋 Все поиски пользователей</h3>${ss}`;
}
async function adminAct(sid, act){
  if (act === 'delete' && !confirm('Удалить поиск №' + sid + ' со всеми лотами?')) return;
  const r = await api(`/api/admin/search/${sid}/${act}`, {});
  if (r.ok) { toast('✅ Готово'); loadAdmin(); } else toast(r.error || 'Ошибка', true);
}
function toggleFromModal(sid, iid){
  toggleCmp(sid, iid);
  loadItems();
  if (state.tab !== 'cmp') toast('⚖️ Добавил в сравнение — вкладка «Сравнение»');
}

// ---------------- лоты ----------------
function verBadge(i){
  if (i.verdict === 'MATCH') return '<span class="badge b-match">✅ совпадение</span>';
  if (i.verdict === 'CLOSE') return '<span class="badge b-close">🟡 возможно</span>';
  if (i.verdict === 'REJECT') return '<span class="badge b-reject">не подходит</span>';
  if (i.status === 'RESERVED') return '<span class="badge b-defect">зарезервирован</span>';
  if (i.status.startsWith('REJECTED') || i.status === 'SCREENED_NO') return '<span class="badge b-defect">отсеян</span>';
  if (i.status === 'NO_DATA') return '<span class="badge b-nodata">нет данных</span>';
  return '';
}
function cmpOn(i){ return state.cmp.includes(i.search_id+':'+i.avito_id); }
async function loadItems(){
  const box = document.getElementById('items-list');
  const q = new URLSearchParams({mode: state.mode});
  if (state.searchFilter) q.set('search_id', state.searchFilter);
  if (state.q) q.set('q', state.q);
  let items;
  try { items = await api('/api/items?'+q); } catch(e){ return; }
  state.itemsCache = items;
  loadChips(); // обновляем фильтры-чипсы (если нет — покажется только «Все поиски»)
  if (!items.length){
    box.innerHTML = '<div class="ghosting"><b>📭</b>Ничего не найдено.<br>Начни поиск на вкладке «➕ Новый» или переключи фильтр.</div>';
    return;
  }
  box.innerHTML = items.map(i=>{
    const inCmp = cmpOn(i);
    return `<div class="card item-card ${i.hidden?'hidden-item':''}" onclick="openItem(${i.search_id},'${esc(i.avito_id)}')">
      <div class="acts">
        <button class="it-btn ${i.favorite?'on':''}" onclick="event.stopPropagation();toggleFlag('${i.search_id}','${esc(i.avito_id)}','favorite')" title="В избранное">⭐</button>
        <button class="it-btn cmp ${inCmp?'on':''}" onclick="event.stopPropagation();toggleCmp('${i.search_id}','${esc(i.avito_id)}',this)" title="В сравнение">⚖️</button>
        <button class="it-btn ${i.hidden?'on':'danger'}" onclick="event.stopPropagation();toggleFlag('${i.search_id}','${esc(i.avito_id)}','hidden')" title="Скрыть/вернуть">👁</button>
      </div>
      <div style="font-size:14.5px;font-weight:750;line-height:1.35">${esc(i.title)}</div>
      <div style="margin:5px 0 2px;display:flex;align-items:center;gap:8px;flex-wrap:wrap"><span class="pricing">${rub(i.price)}</span> ${verBadge(i)} ${i.favorite?'⭐':''} ${i.hidden?' 👻':''}</div>
      <div class="muted" style="margin:4px 0 2px">${esc((i.reason||i.snippet||'').slice(0,220))}</div>
      <div class="muted">${esc(i.created_at||'')} · поиск «${esc(i.search_title)}»</div>
      <a class="lnk" href="${esc(i.url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">Открыть на Авито ↗</a>
    </div>`;
  }).join('');
}
async function toggleFlag(sid, iid, field){
  await api(`/api/items/${sid}/${iid}/flag/${field}`, {});
  loadItems();
}

// ---------------- сравнение ----------------
function toggleCmp(sid, iid, btn){
  const key = sid+':'+iid;
  const idx = state.cmp.indexOf(key);
  if (idx >= 0) { state.cmp.splice(idx, 1); if (btn) btn.classList.remove('on'); }
  else if (state.cmp.length >= 6) { toast('Максимум 6 товаров', true); return; }
  else { state.cmp.push(key); if (btn) btn.classList.add('on'); }
  updateCmpNub();
  if (state.tab === 'cmp') renderCmp();
}
function updateCmpNub(){
  const nub = document.getElementById('cmp-nub');
  nub.textContent = state.cmp.length;
  document.querySelector('#nav .tab[data-tab="cmp"]').classList.toggle('sel', state.cmp.length>0);
}
function clearCmp(){ state.cmp = []; updateCmpNub(); renderCmp(); if (state.tab==='items') loadItems(); }
function renderCmp(){
  const box = document.getElementById('cmp-basket');
  const controls = document.getElementById('cmp-controls');
  const empty = document.getElementById('cmp-empty');
  updateCmpNub();
  if (!state.cmp.length){
    box.innerHTML = ''; controls.style.display = 'none';
    empty.style.display = 'block';
    return;
  }
  empty.style.display = 'none';
  controls.style.display = 'block';
  const rows = state.cmp.map(key=>{
    const [sid, iid] = key.split(':');
    const it = (state.itemsCache||[]).find(i=>i.search_id+':'+i.avito_id===key);
    const title = it ? it.title : 'лот #'+iid;
    const price = it ? it.price : 0;
    return `<div class="pick"><div class="grow"><b style="font-size:13px">${esc(title).slice(0,60)}</b>
      <span class="muted">   · ${rub(price)}</span></div>
      <button class="rm" onclick="rmCmp('${key}')">✕</button></div>`;
  }).join('');
  box.innerHTML = `<div class="card"><h3>⚖️ В сравнении (${state.cmp.length})</h3>${rows}</div>`;
}
function rmCmp(key){
  state.cmp = state.cmp.filter(k=>k!==key);
  updateCmpNub(); renderCmp(); if (state.tab==='items') loadItems();
}
async function startCompare(){
  if (state.cmp.length < 2) { toast('Нужно минимум 2 товара', true); return; }
  const btn = document.getElementById('cmp-go');
  btn.disabled = true;
  const r = await api('/api/compare', {items: state.cmp, note: document.getElementById('cmp-note').value.trim()});
  if (!r.ok) { toast(r.error, true); btn.disabled = false; return; }
  toast('Отправил в нейросеть — следи тут');
  pollCompare();
}
function stopCmpPoll(){
  if (window._cmpPoll) { clearInterval(window._cmpPoll); window._cmpPoll = null; }
}
async function pollCompare(){
  let c;
  try { c = await api('/api/compare'); } catch(e){ return; }
  const box = document.getElementById('cmp-box');
  if (c.state === 'processing'){
    box.innerHTML = `<div class="card"><div id="cmpline"><span class="spinner"></span> Снимаю карточки с Авито и спрашиваю нейросеть…</div></div>`;
    stopCmpPoll();
    window._cmpPoll = setInterval(async ()=>{ try{ await pollCompare(); }catch(e){} }, 3000);
    return;
  }
  stopCmpPoll();
  document.getElementById('cmp-go').disabled = false;
  if (c.state === 'error'){ box.innerHTML = `<div class="card"><b style="color:var(--bad)">:( ${esc(c.error||'Ошибка')}</b></div>`; return; }
  if (c.state === 'done' && c.result){
    const v = c.result;
    const details = (v.details||[]).map(d=>`<li style="margin:5px 0;color:#c4cbd8;font-size:13px">${esc(d)}</li>`).join('');
    box.innerHTML = `<div class="cmp-result">
      <div class="card winner"><h4>🏆 Вариант №${v.winner+1} — лучший по мнению нейросети</h4>
        <div class="muted" style="margin-bottom:6px">${esc((c.items[v.winner]||{}).title||'')}</div>
        <div style="font-size:15px;font-weight:750;line-height:1.5">${esc(v.summary)}</div></div>
      <details open><summary>Почему → ${esc((c.items[v.winner]||{}).title||'').slice(0,50)}</summary>
        <div class="why">${esc(v.why)}</div></details>
      <details><summary>Разбор всех вариантов</summary><ul>${details}</ul></details>
      <div class="tips">💡 ${esc(v.tips)}</div>
    </div>`;
  }
  if (c.state === 'idle') { box.innerHTML=''; }
}

// ---------------- поиски ----------------
async function loadSearches(){
  await loadChips();
  const box = document.getElementById('searches-list');
  if (!state.searches.length){ box.innerHTML = '<div class="ghosting"><b>🔍</b>Поисков ещё нет.<br>Создай первый на вкладке «➕ Новый».</div>'; return; }
  box.innerHTML = state.searches.map(s=>{
    const bm = {ACTIVE:'b-active',PAUSED:'b-paused',STOPPED:'b-stopped',ASKING:'b-asking',PLANNING:'b-planning',PLANNING_FAILED:'b-reject'};
    const p = s.profile || {};
    const price = (p.price_min||p.price_max) ? `${rub(p.price_min)} — ${rub(p.price_max)}` : 'цена не задана';
    const cities = (p.city_slugs||[]).map(cityName).join(', ') || 'города не выбраны';
    const del = p.delivery==='only' ? ' · 🚚 Авито Доставка' : (p.delivery==='off' ? ' · 🏙 без доставки' : '');
    const must = (p.must||[]).map(m=>`<li>${esc(m)}</li>`).join('');
    const no = (p.must_not||[]).map(m=>`<li class="no">🚫 ${esc(m)}</li>`).join('');
    let acts = '';
    if (s.status==='ACTIVE') acts += `<button class="it-btn" style="width:auto;padding:0 14px" onclick="sact(${s.id},'pause')">⏸ Пауза</button>`;
    if (s.status==='PAUSED') acts += `<button class="it-btn" style="width:auto;padding:0 14px" onclick="sact(${s.id},'resume')">▶️ Возобновить</button>`;
    if (s.status==='STOPPED') acts += `<button class="it-btn" style="width:auto;padding:0 14px" onclick="sact(${s.id},'resume')">▶️</button>`;
    if (s.status==='PLANNING_FAILED') acts += `<button class="it-btn" style="width:auto;padding:0 14px" onclick="sact(${s.id},'replan')">🔄 Заново</button>`;
    if (s.status!=='STOPPED' && s.status!=='ASKING' && s.status!=='PLANNING') acts += `<button class="it-btn danger" style="width:auto;padding:0 14px" onclick="sact(${s.id},'stop')">🛑 Завершить</button>`;
    acts += `<button class="it-btn danger" onclick="sdel(${s.id})">🗑</button>`;
    const asks = (s.status==='ASKING'||s.status==='PLANNING');
    return `<div class="card">
      <div class="row">
        <div class="grow">
          <div><b>№${s.id} ${esc(s.title)}</b> <span class="badge ${bm[s.status]||'b-nodata'}">${ruStatus(s.status)}</span></div>
          <div class="muted" style="margin:5px 0">${price} · 📍 ${esc(cities)}${del}</div>
          <ul class="must">${must}${no}</ul>
          <div class="muted" style="margin-top:7px">лотів: ${s.items_count} · показано: ${s.matched_count} · обновлён: ${esc(s.last_scan_at||'—')}</div>
        </div>
        ${asks?'':`<div class="acts">${acts}</div>`}
      </div>
    </div>`;
  }).join('');
  // если поиск “уточняется” — подскажем, что диалог на вкладке Новый
  const asking = state.searches.find(s=>s.status==='ASKING'||s.status==='PLANNING');
  if (asking) box.insertAdjacentHTML('afterbegin',
    `<div class="card" style="border-color:var(--violet)"><b>🧠 Поиск №${asking.id} сейчас формируется/уточняется</b>
     <div class="muted" style="margin-top:4px">Открыт диалог на вкладке «➕ Новый» — два поиска одновременно запустить нельзя.</div></div>`);
}
function ruStatus(st){
  return {ACTIVE:'активен',PAUSED:'пауза',STOPPED:'остановлен',ASKING:'уточняем',PLANNING:'формируем',PLANNING_FAILED:'ошибка'}[st]||st;
}
function cityName(slug){
  const m = {"rossiya":"Россия","novokuznetsk":"Новокузнецк","kemerovo":"Кемерово","novosibirsk":"Новосибирск","moskva":"Москва","spb":"СПб"};
  return m[slug] || slug;
}
async function sact(id, act){
  await api(`/api/searches/${id}/${act}`, {});
  loadSearches(); toast('Готово');
}
async function sdel(id){
  if (!confirm('Удалить поиск №'+id+' вместе с его лотами?')) return;
  await api(`/api/searches/${id}/delete`, {});
  await loadSearches();
  if (state.searchFilter===id){ state.searchFilter = null; loadItems(); }
  toast('Удалён');
}

// ---------------- создание поиска ----------------
async function createSearch(){
  const text = document.getElementById('new-text').value.trim();
  const btn = document.getElementById('new-btn');
  const r = await api('/api/search', {text}).catch(e=>null);
  if (!r || !r.ok){ toast(r?.error || 'Ошибка', true); return; }
  state.sid = r.sid;
  btn.disabled = true;
  document.getElementById('new-text').value = '';
  await pollCreation(true);
  state.pollNew = setInterval(pollCreation, 2500);
}
async function pollCreation(){
  let s;
  try { s = await api('/api/search/'+state.sid); } catch(e){ return; }
  const box = document.getElementById('new-status');
  const btn = document.getElementById('new-btn');
  if (s.status === 'PLANNING'){
    box.innerHTML = `<div class="card"><h3>🧠 Нейросеть собирает план…</h3>
      <p class="muted"><span class="spinner"></span> Запросы к нейросети встают в очередь (лимит ~63 сек) — это может занять пару минут.</p></div>`;
    return;
  }
  if (s.status === 'PLANNING_FAILED'){
    btn.disabled = false;
    box.innerHTML = `<div class="card"><h3>😵 Не удалось собрать план</h3>
      <div style="margin-top:10px"><button class="btn primary" onclick="replan(${s.id})">🔄 Попробовать снова</button></div></div>`;
    stopPollers(); loadChips();
    return;
  }
  if (['ACTIVE','PAUSED','STOPPED'].includes(s.status)){
    stopPollers(); btn.disabled = false;
    box.innerHTML = '';
    toast('🚀 Поиск запущен!');
    loadChips();
    setTab('items');
    return;
  }
  if (s.status === 'ASKING'){
    stopPollers();
    btn.disabled = false;
    renderDialog(s);
  }
}
async function replan(sid){
  await sact(sid, 'replan');
  state.sid = sid;
  pollCreation();
  state.pollNew = setInterval(pollCreation, 2500);
}
async function renderDialog(s){
  const idx = (s.answers||[]).length;
  const qs = s.questions || [];
  const box = document.getElementById('new-status');
  const answered = (s.answers||[]).map(a=>`<div class="muted">✅ <b>${esc(a.question)}:</b> ${esc(a.answer)}</div>`).join('');
  if (idx >= qs.length){
    box.innerHTML = `<div class="card"><h3>🧠 Учитываю ответы…</h3><p class="muted"><span class="spinner"></span></p>${answered}</div>`;
    state.pollNew = setInterval(pollCreation, 3000);
    return;
  }
  const q = qs[idx];
  state.curQ = q;
  const btns = (q.options||[]).map((o,i)=>`<button class="chip" style="margin:3px" onclick="answer(${idx},${i})">${esc(o)}</button>`).join(' ');
  box.innerHTML = `<div class="card">
    <h3>${esc(q.question)}</h3>
    ${answered}
    <div class="chips" style="position:static;background:none;padding:10px 0 0">${btns}</div>
    <input id="free-ans" placeholder="Или напиши свой вариант и нажми Enter" onkeydown="if(event.key==='Enter')freeAnswer(${idx})">
    <div style="margin-top:10px"><button class="btn ghost" onclick="skipDialog()">Пропустить остальные</button></div>
  </div>`;
}
async function answer(idx, optI){
  const opt = (state.curQ && state.curQ.options) ? state.curQ.options[optI] : String(optI);
  const r = await api(`/api/searches/${state.sid}/answer`, {idx, answer: opt});
  if (!r.ok){ toast(r.error, true); return; }
  await afterAnswer(r);
}
async function freeAnswer(idx){
  const el = document.getElementById('free-ans');
  const v = (el.value||'').trim();
  if (!v) return;
  const r = await api(`/api/searches/${state.sid}/answer`, {idx, answer: v});
  if (!r.ok){ toast(r.error, true); return; }
  await afterAnswer(r);
}
async function afterAnswer(r){
  if (r.done) { toast('🚀 Учитываю ответы и запускаю поиск…'); await pollCreation(); state.pollNew = setInterval(pollCreation, 3000); return; }
  let s = await api('/api/search/'+state.sid);
  renderDialog(s);
}
async function skipDialog(){
  const r = await api(`/api/searches/${state.sid}/skip`, {});
  if (!r.ok){ toast(r.error, true); return; }
  toast('🚀 Запускаю с собранными данными…');
  await pollCreation();
  state.pollNew = setInterval(pollCreation, 3000);
}

startBoot();
</script>
</body>
</html>"""


async def index(_):
    return web.Response(text=PAGE, content_type="text/html", charset="utf-8")


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_post("/api/auth/tg", auth_tg)
    app.router.add_post("/api/auth/code", auth_code)
    app.router.add_get("/api/me", me)
    app.router.add_get("/api/items", api_items)
    app.router.add_get("/api/items/{sid}/{iid}", api_item_card)
    app.router.add_post("/api/items/{sid}/{iid}/flag/{field}", api_flag)
    app.router.add_post("/api/items/{sid}/{iid}/request", api_item_card_request)
    app.router.add_get("/api/searches", api_searches)
    app.router.add_get("/api/search/{sid}", api_search_get)
    app.router.add_post("/api/search", api_search_create)
    app.router.add_post("/api/search/{sid}/answer", api_search_answer)
    app.router.add_post("/api/search/{sid}/skip", api_search_skip)
    app.router.add_get("/api/compare", api_compare_status)
    app.router.add_post("/api/compare", api_compare_start)
    app.router.add_post("/api/searches/{sid}/{action}", api_search_action)
    app.router.add_get("/api/admin", api_admin)
    app.router.add_post("/api/admin/search/{sid}/{action}", api_admin_search_action)
    app.router.add_post("/api/admin/queue/clear", api_admin_queue_clear)
    return app


async def start_server():
    port = int(appconfig.cfg("web_port", 8080))
    runner = web.AppRunner(build_app())
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    log.info(f"🌐 mini-app слушает http://0.0.0.0:{port}")
    asyncio.create_task(_card_worker())
    return runner
