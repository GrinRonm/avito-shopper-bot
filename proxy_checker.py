"""
Чекер бесплатных прокси для пула сканирования Авито.

Источники:
  - geonode API (RU, http/https)
  - TheSpeedX/SOCKS-List http.txt (все страны)

Проверка каждого кандидата:
  1) живость: HTTPS-запрос через CONNECT (api.ipify.org), таймаут ~8 c
  2) пригодность для Авито: GET https://www.avito.ru/robots.txt
     (iban-page «Доступ ограничен» / 429 / нет ответа -> не годится)

Результат:
  - консольный отчёт
  - годные прокси в data/proxies_good.txt (схема://host:port[@user:pass] + country/latency в комментарии)

Запуск:
  python proxy_checker.py [--limit-speedx 400] [--threads 60] [--avito-max 40]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import urllib.request

import aiohttp

GEONODE_URL = ("https://proxylist.geonode.com/api/proxy-list"
               "?country=RU&protocols=http%2Chttps&limit=500&sort_by=responseTime&sort_type=asc")
SPEEDX_URL = "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/http.txt"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_FILE = os.path.join(HERE, "data", "proxies_good.txt")


def load_candidates(limit_speedx: int) -> list[dict]:
    out: list[dict] = []
    # 1) geonode (с authorisation-логином/паролем если есть)
    try:
        req = urllib.request.Request(GEONODE_URL, headers={"User-Agent": UA})
        data = json.loads(urllib.request.urlopen(req, timeout=20).read())
        for p in data.get("data", []):
            host, port = p.get("ip"), p.get("port")
            if not host or not port:
                continue
            auth = ""
            if p.get("username"):
                auth = f"{p['username']}:{p.get('password', '')}@"
            out.append({
                "url": f"http://{auth}{host}:{port}",
                "country": p.get("country") or "?",
                "latency": p.get("responseTime") or -1,
            })
    except Exception as e:
        print(f"[geonode] не удалось получить список: {e}", file=sys.stderr)
    # 2) SpeedX http.txt
    try:
        req = urllib.request.Request(SPEEDX_URL, headers={"User-Agent": UA})
        lines = urllib.request.urlopen(req, timeout=30).read().decode().split()
        lines = [l.strip() for l in lines if ":" in l.strip()]
        random.shuffle(lines)
        for l in lines[:limit_speedx]:
            out.append({"url": f"http://{l}", "country": "?", "latency": -1})
    except Exception as e:
        print(f"[speedx] не удалось получить список: {e}", file=sys.stderr)
    # уникальные
    seen, uniq = set(), []
    for c in out:
        key = c["url"].split("@")[-1]
        if key in seen:
            continue
        seen.add(key)
        uniq.append(c)
    return uniq


async def check(session: aiohttp.ClientSession, c: dict, sem: asyncio.Semaphore,
                do_avito: bool, results: dict):
    async with sem:
        proxy = c["url"]
        exit_ip, country = None, c.get("country", "?")
        # 1) живость + выходной IP
        try:
            async with session.get("https://api.ipify.org?format=json",
                                   proxy=proxy, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return
                exit_ip = (await resp.json()).get("ip")
        except Exception:
            return
        if not exit_ip:
            return
        results["alive"] += 1
        if not do_avito:
            results["alive_list"].append({**c, "exit_ip": exit_ip})
            return
        # 2) авито: robots.txt — дешёвый запрос; заглушки/429 = не годится
        try:
            async with session.get("https://www.avito.ru/robots.txt", proxy=proxy,
                                   headers={"User-Agent": UA},
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
                body = (await resp.text())[:400].lower()
                if resp.status == 200 and "доступ ограничен" not in body:
                    results["avito_ok"] += 1
                    results["good"].append({**c, "exit_ip": exit_ip})
                elif resp.status == 429:
                    results["avito_429"] += 1
                else:
                    results["avito_blocked"] += 1
        except Exception:
            results["avito_error"] += 1


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit-speedx", type=int, default=400)
    ap.add_argument("--threads", type=int, default=60)
    ap.add_argument("--avito-max", type=int, default=40,
                    help="сколько из живых проверять против Авито (остальные только alive)")
    args = ap.parse_args()

    candidates = load_candidates(args.limit_speedx)
    print(f"кандидатов: {len(candidates)} (потоков {args.threads})")
    results = {"alive": 0, "avito_ok": 0, "avito_429": 0, "avito_blocked": 0,
               "avito_error": 0, "good": [], "alive_list": []}
    sem = asyncio.Semaphore(args.threads)
    connector = aiohttp.TCPConnector(limit=0, ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        await asyncio.gather(*(check(session, c, sem, i < args.avito_max, results)
                               for i, c in enumerate(candidates)))

    print(f"\n== Итоги ==")
    print(f"живых прокси: {results['alive']}")
    print(f"авито-проверено: ok={results['avito_ok']} 429={results['avito_429']} "
          f"блок={results['avito_blocked']} err={results['avito_error']}")
    good = results["good"] or results["alive_list"][:args.avito_max]
    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        for c in good:
            src = "geonode" if c.get("latency", -1) >= 0 else "speedx"
            f.write(f"{c['url']}  # exit={c['exit_ip']} src={src}\n")
    print(f"сохранено в {OUT_FILE}: {len(good)} шт.")
    for c in results["good"][:15]:
        print("  ok:", c["url"].split("@")[-1], "exit=", c["exit_ip"])


if __name__ == "__main__":
    asyncio.run(main())
