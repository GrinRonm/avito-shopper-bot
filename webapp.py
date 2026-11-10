"""Мини-app: aiohttp-сайт со всеми найденными лотами и управлением поисками.
Запускается внутри основного процесса бота (loop общий).
"""
import json
import logging

from aiohttp import web

import appconfig
import db

log = logging.getLogger("webapp")

HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Avito Шопер</title>
<style>
  :root { --accent:#0af; }
  body { font-family: system-ui, Arial, sans-serif; margin:0; background:#f4f6f8; color:#222; }
  header { background:#104a2f; color:#fff; padding:14px 20px; display:flex; gap:20px; align-items:center; }
  header h1 { font-size:18px; margin:0; }
  nav button { background:transparent; border:1px solid #ffffff55; color:#fff; border-radius:8px;
               padding:6px 14px; cursor:pointer; margin-left:6px; }
  nav button.on { background:#ffffff22; border-color:#fff; }
  .wrap { max-width: 1100px; margin:0 auto; padding:16px; }
  .card { background:#fff; border-radius:12px; padding:14px 16px; margin:10px 0;
          box-shadow:0 1px 3px rgba(0,0,0,.08); }
  .row { display:flex; gap:10px; align-items:flex-start; }
  .grow { flex:1; }
  .muted { color:#888; font-size:12px; }
  .badge { display:inline-block; padding:2px 8px; border-radius:10px; font-size:11px; color:#fff; }
  .b-match { background:#0a0; } .b-close { background:#da8b00; } .b-reject { background:#c33; }
  .b-nodata { background:#888; } .b-defect { background:#333; }
  .b-active { background:#0a0; } .b-paused { background:#da8b00; } .b-stopped { background:#c33; }
  .b-asking { background:#06c; }
  .chip { padding:6px 14px; border:1px solid #ccc; border-radius:20px; background:#fff; cursor:pointer; margin:3px 4px 3px 0; }
  .chip.on { background:#104a2f; color:#fff; border-color:#104a2f; }
  .item-btn { border:1px solid #ccc; background:#fff; border-radius:8px; padding:5px 10px; cursor:pointer; margin:2px; }
  .item-btn.on { background:#ffe9a8; border-color:#e0c060; }
  .item-btn.hide-on { background:#ddd; }
  .search-meta { font-size:13px; color:#555; }
  .must { margin:6px 0 0; padding-left:16px; font-size:12px; color:#555; }
  a { color:#0af; text-decoration:none; }
  .hidden-item { opacity:.55; }
  .pricing { font-weight:600; }
  .empty { text-align:center; color:#999; padding:40px 0; }
</style>
</head>
<body>
<header>
  <h1>🛒 Avito Шопер</h1>
  <nav>
    <button id="tab-items" class="on" onclick="showTab('items')">📦 Лоты</button>
    <button id="tab-searches" onclick="showTab('searches')">🔍 Поиски</button>
  </nav>
</header>
<div class="wrap">
  <div id="items">
    <div>
      <button class="chip on" data-mode="notified" onclick="setMode(this,'notified')">Показанные</button>
      <button class="chip" data-mode="favorites" onclick="setMode(this,'favorites')">⭐ Избранное</button>
      <button class="chip" data-mode="hidden" onclick="setMode(this,'hidden')">👻 Скрытые</button>
      <button class="chip" data-mode="rejected" onclick="setMode(this,'rejected')">Отброшенные</button>
      <button class="chip" data-mode="all" onclick="setMode(this,'all')">Все</button>
    </div>
    <div id="items-list"></div>
  </div>
  <div id="searches" style="display:none;"><div id="searches-list"></div></div>
</div>
<script>
let mode = 'notified';

function showTab(t) {
  document.getElementById('items').style.display = t==='items'?'block':'none';
  document.getElementById('searches').style.display = t==='searches'?'block':'none';
  document.getElementById('tab-items').className = t==='items'?'on':'';
  document.getElementById('tab-searches').className = t==='searches'?'on':'';
}
function setMode(el, m) {
  mode = m;
  document.querySelectorAll('.chip').forEach(c=>c.classList.remove('on'));
  el.classList.add('on');
  loadItems();
}
function rub(v){ return v ? Number(v).toLocaleString('ru-RU')+' ₽' : '—'; }
function esc(s){ return (s||'').toString().replace(/&/g,'&amp;').replace(/</g,'&lt;'); }

async function loadItems(){
  const r = await fetch('/api/items?mode='+mode);
  const items = await r.json();
  const box = document.getElementById('items-list');
  if (!items.length) { box.innerHTML = '<div class="empty">Пока пусто 🐤</div>'; return; }
  box.innerHTML = items.map(i => {
    const badgeMap = {MATCH:'b-match', CLOSE:'b-close', REJECT:'b-reject'};
    const statusBadge = {VERIFIED_MATCH:'✅', VERIFIED_CLOSE:'🟡'}
    let badge = i.verdict && badgeMap[i.verdict] ? '<span class="badge '+badgeMap[i.verdict]+'">'+i.verdict+'</span>' : '';
    if (i.status.startsWith('REJECTED') || i.status==='RESERVED' ) badge = '<span class="badge b-defect">'+i.status.replace('REJECTED_','')+'</span>';
    else if (!badge && i.status.startsWith('VERIFIED')) badge = '<span class="badge b-nodata">'+i.status.replace('VERIFIED_','')+'</span>';
    const cls = i.hidden ? 'card hidden-item' : 'card';
    return `<div class="${cls}">
      <div class="row">
        <div class="grow">
          <div><b>${esc(i.title)}</b> ${badge} ${i.favorite?'⭐':''}</div>
          <div class="pricing">${rub(i.price)} · <span class="muted">${esc(i.search_title)}</span></div>
          <div class="muted">${esc(i.reason||'').slice(0,220)}</div>
          <div class="muted">${esc(i.created_at)}</div>
          <a href="${esc(i.url)}" target="_blank">Открыть на Авито ↗</a>
        </div>
        <div>
          <button class="item-btn ${i.favorite?'on':''}" onclick="toggleFlag(${i.search_id},'${i.avito_id}','favorite')">⭐</button>
          <button class="item-btn ${i.hidden?'hide-on':''}" onclick="toggleFlag(${i.search_id},'${i.avito_id}','hidden')">👁</button>
        </div>
      </div>
    </div>`;
  }).join('');
}

async function toggleFlag(sid, iid, field){
  await fetch(`/api/items/${sid}/${iid}/flag/${field}`, {method:'POST'});
  loadItems();
}

async function loadSearches(){
  const r = await fetch('/api/searches');
  const list = await r.json();
  const box = document.getElementById('searches-list');
  if (!list.length) { box.innerHTML = '<div class="empty">Поисков нет</div>'; return; }
  box.innerHTML = list.map(s => {
    const bmap = {ACTIVE:'b-active', PAUSED:'b-paused', STOPPED:'b-stopped', ASKING:'b-asking'};
    const p = s.profile || {};
    const price = p.price_min||p.price_max ? `${rub(p.price_min)}–${rub(p.price_max)}` : 'цена не задана';
    const cities = (p.city_slugs||[]).join(', ');
    const must = (p.must||[]).map(m=>`<li>${esc(m)}</li>`).join('');
    const mustNot = (p.must_not||[]).map(m=>`<li style="color:#c33">🚫 ${esc(m)}</li>`).join('');
    const del = p.delivery==='only' ? '🚚 Авито Доставка' : (p.delivery==='off' ? '🏙 без доставки' : '');
    const acts = s.status==='ACTIVE' ? 'pause' : (s.status==='PAUSED' ? 'resume' : null);
    return `<div class="card">
      <div class="row">
        <div class="grow">
          <div><b>#${s.id} ${esc(s.title)}</b> <span class="badge ${bmap[s.status]||'b-nodata'}">${s.status}</span></div>
          <div class="search-meta">${price} · 📍 ${esc(cities)} ${del?' · '+del:''} · лотов: ${s.items_count}, показано: ${s.matched_count}</div>
          <ul class="must">${must}${mustNot}</ul>
        </div>
        <div>
          ${acts?`<button class="item-btn" onclick="searchAct(${s.id},'${acts}')">${acts==='pause'?'⏸ Пауза':'▶️ Возобновить'}</button>`:''}
          ${s.status==='PAUSED'||s.status==='STOPPED' ? `<button class="item-btn" onclick="searchAct(${s.id},'resume')">▶️</button>`:''}
          ${s.status!=='STOPPED' ? `<button class="item-btn" onclick="searchAct(${s.id},'stop')">🛑</button>`:''}
          <button class="item-btn" onclick="searchAct(${s.id},'delete',true)">🗑 Удалить</button>
        </div>
      </div>
    </div>`;
  }).join('');
}

async function searchAct(sid, act, confirmDel=undefined){
  if (act==='delete' && !confirm('Удалить поиск #'+sid+' со всеми лотами?')) return;
  await fetch(`/api/searches/${sid}/${act}`, {method:'POST'});
  loadSearches();
}

loadItems();
loadSearches();
setInterval(loadItems, 60000);
</script>
</body>
</html>"""


def _wake_scan():
    try:
        import bot
        bot._wake.set()
    except Exception:
        pass


async def index(_):
    return web.Response(text=HTML, content_type="text/html")


def _json_ok(data) -> web.Response:
    return web.Response(text=json.dumps(data, ensure_ascii=False), content_type="application/json")


async def api_items(request):
    mode = request.query.get("mode", "all")
    return _json_ok(db.list_items_web(mode))


async def api_flag(request):
    sid = int(request.match_info["sid"])
    iid = request.match_info["iid"]
    field = request.match_info["field"]
    val = db.toggle_item_flag(sid, iid, field)
    return _json_ok({"ok": val is not None, "value": val})


async def api_searches(_):
    return _json_ok(db.list_searches_web())


async def api_search_action(request):
    sid = int(request.match_info["sid"])
    act = request.match_info["action"]
    if act == "pause":
        db.set_search_status(sid, "PAUSED")
    elif act == "resume":
        db.set_search_status(sid, "ACTIVE")
        _wake_scan()
    elif act == "stop":
        db.set_search_status(sid, "STOPPED")
    elif act == "delete":
        db.delete_search(sid)
    else:
        return web.Response(text=json.dumps({"ok": False, "error": "unknown action"}, ensure_ascii=False), status=400, content_type="application/json")
    return _json_ok({"ok": True})


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/api/items", api_items)
    app.router.add_post("/api/items/{sid}/{iid}/flag/{field}", api_flag)
    app.router.add_get("/api/searches", api_searches)
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
