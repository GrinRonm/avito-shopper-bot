"""Mini-app сайт: aiohttp-сервер.
Авторизация: подпись Telegram WebApp initData либо одноразовый код из бота (/link).
Лоты (фильтры по поиску/статусам, избранное, скрытие, автообновление),
управление поисками и создание поиска с уточнением кнопками.
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
    user = db.list_allowed_users()
    name = next((u["name"] for u in user if u["chat_id"] == request["chat_id"]), "")
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
    return _json(db.list_items_web(mode, chat_id=request["chat_id"], search_id=search_id))


@require_auth
async def api_flag(request):
    sid = int(request.match_info["sid"])
    if not db.get_search_for_user(sid, request["chat_id"]):
        return _json({"ok": False}, status=404)
    val = db.toggle_item_flag(sid, request.match_info["iid"], request.match_info["field"])
    return _json({"ok": val is not None, "value": val})


# ============================================================ страница

PAGE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>🛒 Avito Шопер</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{
  --bg:#0d1117; --panel:#161b22; --panel2:#1d2330; --line:#2a3140;
  --text:#e6e9ef; --muted:#8b94a7; --accent:#3fb26f; --accent2:#3f8cb2;
  --warn:#d29922; --bad:#e5534b; --chip:#222a37;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-family:system-ui,'Segoe UI',Roboto,Arial,sans-serif}
header{position:sticky;top:0;z-index:5;background:linear-gradient(135deg,#0f2027,#134e3a 60%,#1c6b4a);
  padding:14px 18px;display:flex;align-items:center;gap:14px;box-shadow:0 2px 10px rgba(0,0,0,.4)}
header h1{font-size:18px;margin:0;font-weight:700;letter-spacing:.3px}
header .who{margin-left:auto;font-size:12px;color:#cfe9dd}
nav{display:flex;gap:8px}
nav button{background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.18);color:#fff;
  border-radius:10px;padding:7px 16px;cursor:pointer;font-size:14px;transition:.15s}
nav button.on{background:#fff;color:#123d2c;border-color:#fff;font-weight:600}
.wrap{max-width:1000px;margin:0 auto;padding:16px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:14px 16px;margin:10px 0;transition:.15s}
.card:hover{border-color:#3a4456}
.row{display:flex;gap:12px;align-items:flex-start}
.grow{flex:1;min-width:0}
.muted{color:var(--muted);font-size:12px}
.pricing{font-weight:700;font-size:16px;color:#9fe0b7}
.badge{display:inline-block;padding:2px 9px;border-radius:10px;font-size:11px;font-weight:600;color:#fff;vertical-align:2px}
.b-match{background:var(--accent)} .b-close{background:var(--warn)} .b-reject{background:var(--bad)}
.b-nodata{background:#57606a} .b-defect{background:#444c56}
.b-active{background:var(--accent2)} .b-paused{background:var(--warn)} .b-stopped{background:var(--bad)}
.b-asking{background:#58a6ff} .b-planning{background:#8957e5}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0}
.chip{padding:6px 13px;border:1px solid var(--line);border-radius:18px;background:var(--chip);color:var(--muted);cursor:pointer;font-size:13px;transition:.15s}
.chip.on{background:var(--accent);border-color:var(--accent);color:#04120a;font-weight:600}
.item-btn{border:1px solid var(--line);background:var(--panel2);border-radius:9px;padding:5px 11px;cursor:pointer;margin:2px;color:var(--text);font-size:13px}
.item-btn:hover{border-color:#4a5568}
.item-btn.on{background:#f6d15a;border-color:#f6d15a;color:#201a02}
.item-btn.danger:hover{border-color:var(--bad);color:var(--bad)}
a{color:#6cb6ff;text-decoration:none} a:hover{text-decoration:underline}
.hidden-item{opacity:.5}
.empty{text-align:center;color:var(--muted);padding:46px 0;font-size:15px}
.spinner{display:inline-block;width:15px;height:15px;border:2px solid #888;border-top-color:transparent;border-radius:50%;animation:sp .7s linear infinite;vertical-align:-3px}
@keyframes sp{to{transform:rotate(360deg)}}
textarea{width:100%;min-height:96px;background:var(--panel2);color:var(--text);border:1px solid var(--line);border-radius:10px;padding:10px;font:inherit;font-size:15px;resize:vertical}
textarea:focus,input:focus{outline:none;border-color:var(--accent)}
input{background:var(--panel2);color:var(--text);border:1px solid var(--line);border-radius:10px;padding:10px;font:inherit;width:100%}
.primary{background:var(--accent);border:none;color:#04120a;font-weight:700;border-radius:10px;padding:10px 18px;cursor:pointer;font-size:14px;margin-top:10px}
.primary:disabled{opacity:.5;cursor:not-allowed}
.must{margin:6px 0 0;padding-left:18px;font-size:12.5px;color:#c4cbd8;line-height:1.5}
.must .no{color:#f08d8a}
.tag{display:inline-block;background:var(--chip);border-radius:8px;padding:2px 8px;font-size:11px;color:var(--muted);margin-right:5px}
.center{text-align:center}
#login{max-width:380px;margin:80px auto}
.toast{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);background:var(--accent);color:#04120a;
  padding:10px 20px;border-radius:12px;font-weight:600;opacity:0;transition:.3s;z-index:20}
.toast.show{opacity:1}
.toast.err{background:var(--bad);color:#fff}
</style>
</head>
<body>
<header>
  <h1>🛒 Avito Шопер</h1>
  <nav>
    <button id="t-items" class="on" onclick="setTab('items')">📦 Лоты</button>
    <button id="t-searches" onclick="setTab('searches')">🔍 Поиски</button>
    <button id="t-new" onclick="setTab('new')">➕ Новый поиск</button>
  </nav>
  <div class="who" id="who"></div>
</header>

<div id="login" class="wrap">
  <div class="card">
    <h3>Вход</h3>
    <p class="muted">Если открыл сайт из бота (кнопка «🌐 Открыть мини-ап») — вход произойдёт сам.
    Иначе введи код, который выдаст бот командой <b>/link</b>.</p>
    <input id="code" placeholder="6-значный код" inputmode="numeric" maxlength="6">
    <button class="primary" onclick="loginByCode()">Войти</button>
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
    <div class="chips" id="search-chips"><button class="chip on" onclick="setSearchFilter(this,null)">Все поиски</button></div>
    <div id="items-list"></div>
  </div>
  <div id="v-searches" style="display:none"><div id="searches-list"></div></div>
  <div id="v-new" style="display:none">
    <div class="card">
      <h3>Новый поиск</h3>
      <p class="muted">Опиши желаемое обычным текстом, например: <i>«найди ноутбук 16/512, бренды asus и redmi,
      Ryzen но не U-серии, экран 13–15.6, от 40 до 70 тысяч»</i></p>
      <textarea id="new-text" placeholder="ноутбук 16/512, asus/redmi, Ryzen без U, экран 13-15.6, 40-70к"></textarea>
      <button class="primary" id="new-btn" onclick="createSearch()">🧠 Собрать план поиска</button>
      <div id="new-status"></div>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
if (tg) { tg.ready(); tg.expand(); }

let state = { tab:'items', mode:'notified', searchFilter:null, sid:null, searches:[], pollItems:null, pollNew:null };

function rub(v){ return v ? Number(v).toLocaleString('ru-RU')+' ₽' : '—'; }
function esc(s){ return (s??'').toString().replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;'); }
function toast(msg, err){
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.toggle('err', !!err); t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'), 3500);
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
}
function setTab(t){
  state.tab = t; stopPollers();
  ['items','searches','new'].forEach(x=>{
    document.getElementById('v-'+x).style.display = x===t?'block':'none';
    const b = document.getElementById('t-'+x); if(b) b.className = x===t?'on':'';
  });
  if (t==='items'){ loadItems(); state.pollItems = setInterval(loadItems, 8000); }
  if (t==='searches') loadSearches();
  if (t==='new') document.getElementById('new-text').focus();
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
  // телеграм initData
  if (tg && tg.initData) {
    try {
      const r = await api('/api/auth/tg', {initData: tg.initData});
      if (!r.ok) { toast('Не удалось проверить подпись', true); return; }
      if (r.allowed === false) { document.getElementById('login').innerHTML =
        '<div class="card"><h3>⏳ Заявка отправлена</h3><p class="muted">Жди одобрения от владельца бота.</p></div>'; return; }
    } catch(e) {}
  }
  // есть ли уже сессия (cookie)?
  try {
    const r = await api('/api/me');
    startApp(r);
  } catch(e) { /* нет сессии — остаётся форма входа */ }
}

async function startApp(me){
  try { if (!me) me = await api('/api/me'); } catch(e) { logoutUI(); return; }
  document.getElementById('login').style.display='none';
  document.getElementById('app').style.display='block';
  document.getElementById('who').textContent = (me.name || '') + ' · id' + me.chat_id;
  setTab('items');
}

// ---------------- лоты ----------------
function setMode(el, m){
  state.mode = m;
  document.querySelectorAll('#mode-chips .chip').forEach(c=>c.classList.remove('on'));
  el.classList.add('on');
  loadItems();
}
function setSearchFilter(el, sid){
  state.searchFilter = sid;
  document.querySelectorAll('#search-chips .chip').forEach(c=>c.classList.remove('on'));
  el.classList.add('on');
  loadItems();
}
function verBadge(i){
  if (i.verdict === 'MATCH') return '<span class="badge b-match">✅ совпадение</span>';
  if (i.verdict === 'CLOSE') return '<span class="badge b-close">🟡 возможно</span>';
  if (i.verdict === 'REJECT') return '<span class="badge b-reject">Не подходит</span>';
  if (i.status === 'RESERVED') return '<span class="badge b-defect">зарезервирован</span>';
  if (i.status.startsWith('REJECTED') || i.status === 'SCREENED_NO') return '<span class="badge b-defect">отсеян</span>';
  if (i.status === 'NO_DATA') return '<span class="badge b-nodata">нет данных</span>';
  return '';
}
async function loadItems(){
  const q = new URLSearchParams({mode: state.mode});
  if (state.searchFilter) q.set('search_id', state.searchFilter);
  let items;
  try { items = await api('/api/items?'+q); } catch(e){ return; }
  const box = document.getElementById('items-list');
  if (!items.length){
    box.innerHTML = '<div class="empty">Ничего не найдено — начни поиск на вкладке «➕ Новый поиск»</div>';
    return;
  }
  box.innerHTML = items.map(i=>{
    return `<div class="card ${i.hidden?'hidden-item':''}">
      <div class="row">
        <div class="grow">
          <div><span class="tag">#${i.search_id}</span> <b>${esc(i.title)}</b> ${verBadge(i)} ${i.favorite?'⭐':''}</div>
          <div><span class="pricing">${rub(i.price)}</span> <span class="muted">· ${esc(i.search_title)}</span></div>
          <div class="muted">${esc((i.reason||i.snippet||'').slice(0,220))}</div>
          <div class="muted">${esc(i.created_at||'')}</div>
          <a href="${esc(i.url)}" target="_blank">Открыть на Авито ↗</a>
        </div>
        <div style="white-space:nowrap">
          <button class="item-btn ${i.favorite?'on':''}" title="В избранное" onclick="toggleFlag(${i.search_id},'${i.avito_id}','favorite')">⭐</button>
          <button class="item-btn ${i.hidden?'on':''}" title="Скрыть/вернуть" onclick="toggleFlag(${i.search_id},'${i.avito_id}','hidden')">👁</button>
        </div>
      </div>
    </div>`;
  }).join('');
}
async function toggleFlag(sid, iid, field){
  await api(`/api/items/${sid}/${iid}/flag/${field}`, {});
  loadItems();
}

// ---------------- поиски ----------------
async function loadSearches(){
  state.searches = await api('/api/searches');
  // чипы фильтра лотов
  const chips = document.getElementById('search-chips');
  chips.innerHTML = '';
  const all = document.createElement('button');
  all.className = 'chip'+(state.searchFilter===null?' on':'');
  all.textContent = 'Все поиски';
  all.onclick = ()=>setSearchFilter(all, null);
  chips.appendChild(all);
  state.searches.filter(s=>['ACTIVE','PAUSED','ASKING','PLANNING'].includes(s.status)).forEach(s=>{
    const b = document.createElement('button');
    b.className = 'chip'+(state.searchFilter===s.id?' on':'');
    b.textContent = `#${s.id} ${esc(s.title).slice(0,28)}`;
    b.onclick = ()=>setSearchFilter(b, s.id);
    chips.appendChild(b);
  });
  // список
  const box = document.getElementById('searches-list');
  if (!state.searches.length){ box.innerHTML = '<div class="empty">Поисков ещё нет</div>'; return; }
  box.innerHTML = state.searches.map(s=>{
    const bm = {ACTIVE:'b-active',PAUSED:'b-paused',STOPPED:'b-stopped',ASKING:'b-asking',PLANNING:'b-planning',PLANNING_FAILED:'b-reject'};
    const p = s.profile || {};
    const price = (p.price_min||p.price_max) ? `${rub(p.price_min)} — ${rub(p.price_max)}` : 'цена не задана';
    const cities = (p.city_slugs||[]).map(cityName).join(', ') || 'города не выбраны';
    const del = p.delivery==='only' ? ' · 🚚 Авито Доставка' : (p.delivery==='off' ? ' · 🏙 без доставки' : '');
    const must = (p.must||[]).map(m=>`<li>${esc(m)}</li>`).join('');
    const no = (p.must_not||[]).map(m=>`<li class="no">🚫 ${esc(m)}</li>`).join('');
    let acts = '';
    if (s.status==='ACTIVE') acts += `<button class="item-btn" onclick="sact(${s.id},'pause')">⏸ Пауза</button>`;
    if (s.status==='PAUSED') acts += `<button class="item-btn" onclick="sact(${s.id},'resume')">▶️ Возобновить</button>`;
    if (s.status==='STOPPED') acts += `<button class="item-btn" onclick="sact(${s.id},'resume')">▶️</button>`;
    if (s.status==='PLANNING_FAILED') acts += `<button class="item-btn" onclick="sact(${s.id},'replan')">🔄 Заново</button>`;
    if (s.status!=='STOPPED' && s.status!=='ASKING' && s.status!=='PLANNING') acts += `<button class="item-btn danger" onclick="sact(${s.id},'stop')">🛑 Завершить</button>`;
    acts += `<button class="item-btn danger" onclick="sdel(${s.id})">🗑</button>`;
    const asks = (s.status==='ASKING'||s.status==='PLANNING');
    return `<div class="card">
      <div class="row">
        <div class="grow">
          <div><b>#${s.id} ${esc(s.title)}</b> <span class="badge ${bm[s.status]||'b-nodata'}">${s.status}</span></div>
          <div class="muted" style="margin:4px 0">${price} · 📍 ${esc(cities)}${del}</div>
          <ul class="must">${must}${no}</ul>
          <div class="muted" style="margin-top:6px">лотов: ${s.items_count} · показано: ${s.matched_count} · обновлён: ${esc(s.last_scan_at||'—')}</div>
        </div>
        ${asks?'':`<div style="white-space:nowrap">${acts}</div>`}
      </div>
    </div>`;
  }).join('');
  // если поиск в уточнении — покажем ссылку на вкладку «Новый»
  const asking = state.searches.find(s=>s.status==='ASKING'||s.status==='PLANNING');
  if (asking && state.tab!=='new') { /* индикатор */ }
}
function cityName(slug){
  const m = {"rossiya":"Россия","novokuznetsk":"Новокузнецк","kemerovo":"Кемерово","novosibirsk":"Новосибирск","moskva":"Москва","spb":"СПб"};
  return m[slug] || slug;
}
async function sact(id, act){
  await api(`/api/searches/${id}/${act}`, {});
  loadSearches();
  toast('Готово');
}
async function sdel(id){
  if (!confirm('Удалить поиск #'+id+' вместе со лотами?')) return;
  await api(`/api/searches/${id}/delete`, {});
  loadSearches();
  if (state.searchFilter===id) setSearchFilterByFirst();
  toast('Удалён');
}
function setSearchFilterByFirst(){ state.searchFilter=null; loadSearches(); loadItems(); }

// ---------------- создание поиска ----------------
async function createSearch(){
  const text = document.getElementById('new-text').value.trim();
  const btn = document.getElementById('new-btn');
  btn.disabled = true;
  const r = await api('/api/search', {text});
  if (!r.ok){ toast(r.error, true); btn.disabled = false; return; }
  state.sid = r.sid;
  document.getElementById('new-text').value = '';
  await pollCreation(true);
  state.pollNew = setInterval(pollCreation, 2500);
}
async function pollCreation(force){
  const box = document.getElementById('new-status');
  let s;
  try { s = await api('/api/search/'+state.sid); } catch(e){ return; }
  if (s.status === 'PLANNING'){
    box.innerHTML = `<div class="card"><h3>🧠 Нейросеть собирает план…</h3>
      <p class="muted"><span class="spinner"></span> Запросы к нейросети встают в очередь (лимит ~63 сек) — это может занять пару минут.</p></div>`;
    return;
  }
  if (s.status === 'PLANNING_FAILED'){
    box.innerHTML = `<div class="card"><h3>😵 Не удалось собрать план</h3>
      <button class="primary" onclick="sact(${s.id},'replan')">🔄 Попробовать снова</button></div>`;
    if (state.pollNew) { clearInterval(state.pollNew); state.pollNew = null; }
    loadSearches();
    return;
  }
  if (s.status === 'ACTIVE' || s.status === 'PAUSED' || s.status === 'STOPPED'){
    if (state.pollNew) { clearInterval(state.pollNew); state.pollNew = null; }
    box.innerHTML = '';
    toast('🚀 Поиск запущен!');
    setTab('items');
    loadSearches();
    return;
  }
  if (s.status === 'ASKING'){
    if (state.pollNew) { clearInterval(state.pollNew); state.pollNew = null; }
    await renderDialog(s);
  }
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
  const btns = (q.options||[]).map((o,i)=>`<button class="item-btn" style="margin:3px" onclick="answer(${idx},${i})">${esc(o)}</button>`).join('');
  state.curQ = q;
  box.innerHTML = `<div class="card">
    <h3>${esc(q.question)}</h3>
    ${answered}
    <div style="margin:10px 0">${btns}</div>
    <input id="free-ans" placeholder="Или напиши свой вариант и нажми Enter" onkeydown="if(event.key==='Enter')freeAnswer(${idx})">
    <div style="margin-top:10px"><button class="item-btn" onclick="skipDialog()">Пропустить остальные</button></div>
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
    app.router.add_post("/api/items/{sid}/{iid}/flag/{field}", api_flag)
    app.router.add_get("/api/searches", api_searches)
    app.router.add_get("/api/search/{sid}", api_search_get)
    app.router.add_post("/api/search", api_search_create)
    app.router.add_post("/api/search/{sid}/answer", api_search_answer)
    app.router.add_post("/api/search/{sid}/skip", api_search_skip)
    app.router.add_post("/api/searches/{sid}/{action}", api_search_action)
    return app


async def start_server():
    port = int(appconfig.cfg("web_port", 8080))
    runner = web.AppRunner(build_app())
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    log.info(f"🌐 mini-app слушает http://0.0.0.0:{port}")
    return runner
