"""Авито через Playwright (Windows -> Edge, Linux -> Chromium headless):
- scan_listing(page, search) — обходит листинги по городам и поисковым фразам
- inspect_item(page, url)    — заходит в карточку, снимает описание и характеристики
"""
import asyncio
import logging
import os
import platform
import random
import re
import urllib.parse

import appconfig

log = logging.getLogger("avito")

# ротация поисковых фраз между скан-циклами (search_id -> счётчик циклов)
_RR: dict[int, int] = {}


def _browser_executable():
    """Windows -> Edge (человек видит окно, антибот дружелюбнее).
    Linux/сервер -> встроенный Chromium Playwright (headless)."""
    path = appconfig.cfg("edge_path", "")
    if path and platform.system() == "Windows" and os.path.exists(path):
        return path
    return None


async def open_browser_context():
    from playwright.async_api import async_playwright

    is_windows = platform.system() == "Windows"
    if is_windows:
        headless = appconfig.cfg("headless", False)  # на винде можно с окном (Edge)
    else:
        headless = True  # на сервере весь экрана нет — всегда headless
    executable = _browser_executable()

    pw = await async_playwright().start()
    kwargs = {"headless": headless, "args": ["--disable-blink-features=AutomationControlled"]}
    if not is_windows:
        kwargs["args"] += ["--no-sandbox", "--disable-dev-shm-usage"]  # root в Docker/VPS
    if executable:
        kwargs["executable_path"] = executable

    # прокси для обхода бана датацентровых IP (в config.json: proxy_url).
    # ВАЖНО: Chromium не воспринимает user:pass в URL — нужны отдельные поля.
    proxy_url = str(appconfig.cfg("proxy_url", "") or "").strip()
    if proxy_url:
        proxy = {"server": proxy_url}
        if "@" in proxy_url and "://" in proxy_url:
            from urllib.parse import urlsplit
            u = urlsplit(proxy_url)
            proxy["server"] = f"{u.scheme}://{u.hostname}:{u.port}"
            if u.username:
                proxy["username"] = u.username
                proxy["password"] = u.password or ""
        log.info("Браузер через прокси: %s", proxy["server"])
        kwargs["proxy"] = proxy

    browser = await pw.chromium.launch(**kwargs)
    context = await browser.new_context(locale="ru-RU")
    page = await context.new_page()
    await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    return pw, browser, page

# явный брак/мусор, отсекаем без нейросети (по тексту карточки)
LOCAL_DEFECT_KEYWORDS = [
    "на запчасти", "под восстановление", "не включается",
    "залит", "разбит экран", "битая матрица", "треснуто стекло", "полосы по экрану",
    "артефакт", "чёрный экран", "черный экран", "без матрицы", "не заряжается",
    "требует ремонта", "не работает экран", "сгорела", "сгорел", "вздут акб",
    "запчасти", "не работает звук",
]

STOP_WORDS_URL_FILLER = {"cd": "1", "d": "1", "s": "104"}  # сортировка по дате


def build_url(city: str, slug: str | None, query: str, pmin, pmax, page: int, delivery: str = "off") -> str:
    base = f"https://www.avito.ru/{city}"
    if slug:
        base += f"/{slug}"
    params = {"cd": "1", "s": "104"}  # сортировка по дате
    if delivery == "only":
        params["d"] = "1"  # только с Авито Доставкой
    if query:
        params["q"] = query
    if pmin:
        params["pmin"] = str(pmin)
    if pmax:
        params["pmax"] = str(pmax)
    if page > 1:
        params["p"] = str(page)
    return base + "?" + urllib.parse.urlencode(params)


async def close_browser_context(pw, browser):
    try:
        await browser.close()
    finally:
        await pw.stop()


def _item_id_from_href(href: str) -> str:
    m = re.search(r"_(\d+)(?:\?|$)", href or "")
    return m.group(1) if m else (href or "")


async def _extract_card(card) -> dict | None:
    title_el = await card.query_selector('a[data-marker="item-title"]')
    if not title_el:
        return None
    title = (await title_el.inner_text()).strip()
    href = await title_el.get_attribute("href") or ""
    if href and not href.startswith("http"):
        href = "https://www.avito.ru" + href

    item_id = await card.get_attribute("data-item-id") or _item_id_from_href(href)

    price = 0
    meta = await card.query_selector('meta[itemprop="price"]')
    if meta:
        val = await meta.get_attribute("content")
        if val and val.isdigit():
            price = int(val)
    if not price:
        p_span = await card.query_selector('span[data-marker="item-price"]')
        if p_span:
            digits = re.sub(r"[^\d]", "", await p_span.inner_text())
            if digits:
                price = int(digits)

    card_text = await card.inner_text()
    lines = [l.strip() for l in card_text.split("\n") if l.strip()]
    snippet_bits = [l for l in lines if len(l) > 25 and not l.startswith("http")]
    snippet = " | ".join(snippet_bits[:3])
    return {"item_id": item_id, "title": title, "price": price, "url": href, "snippet": snippet}


async def scan_listing(page, search: dict) -> list:
    """Обход листинга по всем городам и всем поисковым фразам профиля.
    Возвращает новые лоты.

    Устойчивость к «доступ ограничен»: Авито троттлит чрезмерные запросы с одного IP,
    поэтому: бюджет запросов за цикл (scan_requests_per_cycle), ротация фраз между
    циклами и мгновенное прекращение цикла при странице-заглушке."""
    profile = search["profile"]
    sid = search["id"]
    first_pass = search.get("_first_pass", False)
    pages = appconfig.cfg("pages_first_pass", 3) if first_pass else appconfig.cfg("pages_regular", 1)
    max_items = appconfig.cfg("max_items_to_screen_per_search", 12)
    budget = max(1, int(appconfig.cfg("scan_requests_per_cycle", 2)))

    cities = profile.get("city_slugs") or appconfig.cfg("default_city_slugs", ["rossiya"])
    slug = profile.get("category_slug")
    queries = [q.strip() for q in (profile.get("search_queries") or [])
               if isinstance(q, str) and q.strip()]
    if not queries:
        q = profile.get("title_query", "")
        queries = [q] if q else [None]
    pmin = profile.get("price_min")
    pmax = profile.get("price_max")

    # ротация: каждый цикл начинаем со следующей фразы
    offset = _RR.get(sid, 0)
    _RR[sid] = offset + 1
    if len(queries) > 1:
        queries = [queries[(offset + i) % len(queries)] for i in range(len(queries))]

    results = []
    used = 0
    throttled = False

    async def fetch(url: str):
        nonlocal used, throttled
        if used >= budget:
            return False  # бюджет исчерпан
        used += 1
        log.info(f"   [{search['title']}] goto #{used}: {url[:110]}")
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(random.uniform(2.5, 4.5))
        try:  # Avito может редиректнуть прямо во время title() — не валим цикл
            t = (await page.title()).lower()
        except Exception:
            t = ""
        if ("доступ ограничен" in t or "проблема с ip" in t
                or "не загрузилась" in t):
            log.info("   ⛔ Avito душит IP — в этом цикле прошёл только частичный заход; досканим в следующий")
            throttled = True
            return True  # страница открылась, но это заглушка
        return True

    for city in cities:
        if throttled:
            break
        for query in queries:
            if throttled:
                break
            for page_num in range(1, pages + 1):
                ok = await fetch(build_url(city, slug, query, pmin, pmax, page_num,
                                           profile.get("delivery", "off")))
                if not ok:
                    break
                if throttled:
                    break
                cards = await page.query_selector_all('div[data-marker="item"]')
                log.info(f"      карточек на странице: {len(cards)}")

                # страница-заглушка/капча даёт 0 карточек — пробуем фолбэк без slug
                if not cards and slug and page_num == 1:
                    fb_url = build_url(city, None, query, pmin, pmax, page_num,
                                       profile.get("delivery", "off"))
                    log.info(f"   [{search['title']}] нет карточек с категорией, фолбэк без slug")
                    if await fetch(fb_url) and not throttled:
                        cards = await page.query_selector_all('div[data-marker="item"]')
                        log.info(f"      карточек после фолбэка: {len(cards)}")

                for card in cards:
                    data = await _extract_card(card)
                    if not data:
                        continue
                    results.append({"search_id": sid, **data})

            if len(results) >= max_items * 2:
                break
        if len(results) >= max_items * 2:
            break

    # оставляем только новые для этого поиска + накладываем локальный ценовой фильтр
    import db
    seen, uniq = set(), []
    for r in results:
        key = r["item_id"]
        if not key or key in seen:
            continue
        seen.add(key)
        if len(uniq) >= max_items:
            break
        if not db.upsert_item(sid, r["item_id"], r["title"], r["price"], r["url"], r["snippet"]):
            continue
        # локальная проверка ценовой вилки
        if pmin and r["price"] and r["price"] < pmin:
            db.update_item(sid, r["item_id"], status="REJECTED_LOCAL", reason=f"Цена ниже {pmin} ₽")
            continue
        if pmax and r["price"] and r["price"] > pmax:
            db.update_item(sid, r["item_id"], status="REJECTED_LOCAL", reason=f"Цена выше {pmax} ₽")
            continue
        uniq.append(r)
    return uniq


async def inspect_item(page, url: str = None):
    """Заходит в карточку: полное описание, характеристики, фото, резерв.
    Ждёт отрисовку характеристик (два блока с data-marker="item-view/item-params")."""
    out = {"is_reserved": False, "description": "", "params": [], "photos": [], "fetched_at": "", "defect": None}

    for attempt in (1, 2):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=25000)
            # Авито на всякий случай: страница-заглушка «доступ ограничен»
            try:
                t = (await page.title()).lower()
            except Exception:
                t = ""
            if "доступ ограничен" in t or "проблема с ip" in t:
                log.info("   ⛔ карточка не открылась: Avito ограничил IP")
                return out
            # ждём, пока дорисуются характеристики или описание
            got_params = await page.wait_for_selector(
                'div[data-marker="item-view/item-params"] li', timeout=10000)
            if not got_params:
                await page.wait_for_selector(
                    'div[data-marker="item-view/item-description"]', timeout=5000)
            await asyncio.sleep(random.uniform(0.8, 1.5))

            page_content = await page.content()
            if "зарезервирован" in page_content.lower():
                out["is_reserved"] = True
                return out

            desc_el = await page.query_selector('div[data-marker="item-view/item-description"]')
            if desc_el:
                out["description"] = (await desc_el.inner_text()).strip()

            param_nodes = await page.query_selector_all(
                'div[data-marker="item-view/item-params"] li, li[class*="params-paramsList__item"]'
            )
            out["params"] = []
            for node in param_nodes:
                txt = (await node.inner_text()).strip()
                if txt and "\n" not in txt[:30]:
                    out["params"].append(txt.replace("\n", " ").strip())

            # фото из галереи: только крупные картинки Авито (не лого/бейджи/шрифты)
            srcs = await page.evaluate(
                """() => Array.from(document.querySelectorAll('img'))
                    .filter(i => i.src.includes('img.avito.st/image/')
                                 && (i.naturalWidth >= 250 || !i.complete))
                    .slice(0, 6).map(i => i.src)"""
            )
            out["photos"] = list(dict.fromkeys(srcs))

            if out["description"] or out["params"]:
                from datetime import datetime
                out["fetched_at"] = datetime.now().isoformat(timespec="seconds")
                break
            if attempt == 1:
                # пусто — возможно страница дорисовывалась, пробуем ещё раз
                await asyncio.sleep(3)
        except Exception as e:
            log.info(f"   ⚠️ попытка {attempt}: карточка не открылась: {e}")
            if attempt == 1:
                await asyncio.sleep(3)

    combined = (out["description"] + " " + " ".join(out["params"])).lower()
    for kw in LOCAL_DEFECT_KEYWORDS:
        if kw in combined:
            out["defect"] = kw
            break
    return out
