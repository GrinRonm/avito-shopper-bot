"""SQLite-хранилище: поиски, найденные лоты, отправленные уведомления."""
import appconfig
import json
import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shopper.db")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS searches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            title TEXT DEFAULT '',
            raw_request TEXT DEFAULT '',
            profile TEXT DEFAULT '{}',
            questions TEXT DEFAULT '[]',
            answers TEXT DEFAULT '[]',
            status TEXT DEFAULT 'ASKING',
            last_scan_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            closed_at TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS items (
            search_id INTEGER NOT NULL,
            avito_id TEXT NOT NULL,
            title TEXT DEFAULT '',
            price INTEGER DEFAULT 0,
            url TEXT DEFAULT '',
            snippet TEXT DEFAULT '',
            status TEXT DEFAULT 'NEW',
            verdict TEXT DEFAULT '',
            reason TEXT DEFAULT '',
            ai_json TEXT DEFAULT '',
            notified INTEGER DEFAULT 0,
            favorite INTEGER DEFAULT 0,
            hidden INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (search_id, avito_id)
        );
        CREATE TABLE IF NOT EXISTS notifications (
            message_id INTEGER PRIMARY KEY,
            search_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            avito_id TEXT DEFAULT '',
            summary TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS allowed_users (
            chat_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            name TEXT DEFAULT '',
            approved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS access_requests (
            chat_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            name TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            resolved INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS auth_codes (
            chat_id INTEGER PRIMARY KEY,
            code TEXT NOT NULL,
            expires_at TIMESTAMP
        );
    """)
    # миграция на старой базе: новые колонки items
    cols = [r[1] for r in conn.execute("PRAGMA table_info(items)").fetchall()]
    for col in ("favorite", "hidden"):
        if col not in cols:
            conn.execute(f"ALTER TABLE items ADD COLUMN {col} INTEGER DEFAULT 0")
    if "card_json" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN card_json TEXT DEFAULT ''")
    conn.commit()
    conn.close()


# ---------- поиски ----------

def create_search(chat_id: int, title: str, raw_request: str, profile: dict, questions: list) -> int:
    conn = _conn()
    cur = conn.execute(
        "INSERT INTO searches (chat_id, title, raw_request, profile, questions, status) VALUES (?, ?, ?, ?, ?, 'ASKING')",
        (chat_id, title, raw_request, json.dumps(profile, ensure_ascii=False), json.dumps(questions, ensure_ascii=False)),
    )
    sid = cur.lastrowid
    conn.commit()
    conn.close()
    return sid


def get_search(sid: int) -> dict | None:
    conn = _conn()
    row = conn.execute("SELECT * FROM searches WHERE id = ?", (sid,)).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["profile"] = json.loads(d["profile"] or "{}")
    d["questions"] = json.loads(d["questions"] or "[]")
    d["answers"] = json.loads(d["answers"] or "[]")
    return d


def set_search_status(sid: int, status: str):
    conn = _conn()
    extra = ", closed_at = CURRENT_TIMESTAMP" if status in ("STOPPED", "DONE") else ""
    conn.execute(f"UPDATE searches SET status = ?{extra} WHERE id = ?", (status, sid))
    conn.commit()
    conn.close()


def update_search_profile(sid: int, profile: dict, answers: list | None = None):
    conn = _conn()
    if answers is None:
        conn.execute("UPDATE searches SET profile = ? WHERE id = ?", (json.dumps(profile, ensure_ascii=False), sid))
    else:
        conn.execute(
            "UPDATE searches SET profile = ?, answers = ? WHERE id = ?",
            (json.dumps(profile, ensure_ascii=False), json.dumps(answers, ensure_ascii=False), sid),
        )
    conn.commit()
    conn.close()


def append_answer(sid: int, answer: dict):
    """answer: {"question": "...", "answer": "..."}"""
    s = get_search(sid)
    answers = s["answers"] if s else []
    answers.append(answer)
    conn = _conn()
    conn.execute("UPDATE searches SET answers = ? WHERE id = ?", (json.dumps(answers, ensure_ascii=False), sid))
    conn.commit()
    conn.close()


def mark_scanned(sid: int):
    conn = _conn()
    conn.execute("UPDATE searches SET last_scan_at = CURRENT_TIMESTAMP WHERE id = ?", (sid,))
    conn.commit()
    conn.close()


def list_active_searches() -> list:
    conn = _conn()
    rows = conn.execute("SELECT id FROM searches WHERE status = 'ACTIVE' ORDER BY id").fetchall()
    conn.close()
    return [r["id"] for r in rows]


def list_searches_by_chat(chat_id: int, limit: int = 10) -> list:
    conn = _conn()
    rows = conn.execute(
        "SELECT id, title, status, profile FROM searches WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
        (chat_id, limit),
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["profile"] = json.loads(d["profile"] or "{}")
        out.append(d)
    return out


def list_asking_searches(chat_id: int) -> list:
    conn = _conn()
    rows = conn.execute("SELECT id FROM searches WHERE chat_id = ? AND status = 'ASKING'", (chat_id,)).fetchall()
    conn.close()
    return [r["id"] for r in rows]


# ---------- лоты ----------

def upsert_item(sid: int, avito_id: str, title: str, price: int, url: str, snippet: str) -> bool:
    """True если лот новый для этого поиска."""
    conn = _conn()
    cur = conn.execute(
        "INSERT OR IGNORE INTO items (search_id, avito_id, title, price, url, snippet) VALUES (?, ?, ?, ?, ?, ?)",
        (sid, str(avito_id), title, price, url, snippet),
    )
    inserted = cur.rowcount > 0
    conn.commit()
    conn.close()
    return inserted


def update_item(sid: int, avito_id: str, status: str = None, verdict: str = "", reason: str = "", ai_json: str = "", notified: int = None):
    conn = _conn()
    sets, vals = [], []
    if status is not None:
        sets.append("status = ?"); vals.append(status)
    if verdict:
        sets.append("verdict = ?"); vals.append(verdict)
    if reason:
        sets.append("reason = ?"); vals.append(reason)
    if ai_json:
        sets.append("ai_json = ?"); vals.append(ai_json)
    if notified is not None:
        sets.append("notified = ?"); vals.append(1 if notified else 0)
    if not sets:
        conn.close()
        return
    vals.append(sid); vals.append(str(avito_id))
    conn.execute(f"UPDATE items SET {', '.join(sets)} WHERE search_id = ? AND avito_id = ?", vals)
    conn.commit()
    conn.close()


# ---------- уведомления ----------

def add_notification(message_id: int, sid: int, chat_id: int, avito_id: str, summary: str):
    conn = _conn()
    conn.execute(
        "INSERT OR REPLACE INTO notifications (message_id, search_id, chat_id, avito_id, summary) VALUES (?, ?, ?, ?, ?)",
        (message_id, sid, chat_id, str(avito_id), summary),
    )
    conn.commit()
    conn.close()


def get_notification(message_id: int) -> dict | None:
    conn = _conn()
    row = conn.execute("SELECT * FROM notifications WHERE message_id = ?", (message_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def hide_item_forever(search_id: int, avito_id: str, reason: str = ""):
    """Лот, отклонённый реплаем, больше не показывается и не предлагается."""
    conn = _conn()
    conn.execute(
        "UPDATE items SET hidden = 1, status = 'REJECTED_FEEDBACK', reason = ? "
        "WHERE search_id = ? AND avito_id = ?",
        (reason, search_id, str(avito_id)),
    )
    conn.commit()
    conn.close()


# ---------- флаги лотов / удаление поиска (для сайта) ----------

def toggle_item_flag(sid: int, avito_id: str, field: str) -> int | None:
    """field: favorite | hidden. Возвращает новое значение (0/1) или None."""
    if field not in ("favorite", "hidden"):
        return None
    conn = _conn()
    row = conn.execute(f"SELECT {field} FROM items WHERE search_id = ? AND avito_id = ?",
                       (sid, str(avito_id))).fetchone()
    if not row:
        conn.close()
        return None
    new_val = 0 if row[field] else 1
    conn.execute(f"UPDATE items SET {field} = ? WHERE search_id = ? AND avito_id = ?",
                 (new_val, sid, str(avito_id)))
    conn.commit()
    conn.close()
    return new_val


def delete_search(sid: int) -> bool:
    conn = _conn()
    conn.execute("DELETE FROM notifications WHERE search_id = ?", (sid,))
    conn.execute("DELETE FROM items WHERE search_id = ?", (sid,))
    cur = conn.execute("DELETE FROM searches WHERE id = ?", (sid,))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


# ---------- доступы (заявки) ----------

def is_allowed(chat_id: int) -> bool:
    """Конфиговские chat_id + подтверждённые через бота."""
    try:
        if chat_id in set(appconfig.cfg("allowed_chat_ids", [])):
            return True
    except Exception:
        pass
    conn = _conn()
    row = conn.execute("SELECT 1 FROM allowed_users WHERE chat_id = ?", (chat_id,)).fetchone()
    conn.close()
    return row is not None


def add_allowed(chat_id: int, username: str = "", name: str = ""):
    conn = _conn()
    conn.execute(
        "INSERT OR REPLACE INTO allowed_users (chat_id, username, name) VALUES (?, ?, ?)",
        (chat_id, username, name),
    )
    conn.execute("UPDATE access_requests SET resolved = 1 WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()


def deny_user(chat_id: int):
    conn = _conn()
    conn.execute("UPDATE access_requests SET resolved = 1 WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()


def upsert_access_request(chat_id: int, username: str = "", name: str = "") -> bool:
    """True если заявка новая (не требовала отдельного запроса ранее)."""
    conn = _conn()
    row = conn.execute("SELECT chat_id, resolved FROM access_requests WHERE chat_id = ?", (chat_id,)).fetchone()
    if row and not row["resolved"]:
        conn.close()
        return False  # заявка уже висит
    conn.execute(
        "INSERT OR REPLACE INTO access_requests (chat_id, username, name, resolved) VALUES (?, ?, ?, 0)",
        (chat_id, username, name),
    )
    conn.commit()
    conn.close()
    return True


def remove_allowed(chat_id: int) -> bool:
    conn = _conn()
    cur = conn.execute("DELETE FROM allowed_users WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def get_access_request(chat_id: int) -> dict | None:
    conn = _conn()
    row = conn.execute("SELECT * FROM access_requests WHERE chat_id = ?", (chat_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_questions(sid: int, questions: list):
    conn = _conn()
    conn.execute("UPDATE searches SET questions = ? WHERE id = ?", (json.dumps(questions, ensure_ascii=False), sid))
    conn.commit()
    conn.close()


def list_allowed_users() -> list:
    conn = _conn()
    rows = conn.execute("SELECT chat_id, username, name FROM allowed_users").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------- выборки для mini-app ----------

WEB_ITEM_SELECT = """
SELECT i.search_id, i.avito_id, i.title, i.price, i.url, i.snippet, i.status,
       i.verdict, i.reason, i.ai_json, i.notified, i.favorite, i.hidden, i.created_at,
       s.title AS search_title, s.status AS search_status
FROM items i JOIN searches s ON s.id = i.search_id
"""


def list_items_web(mode: str = "all", chat_id: int = None, search_id: int = None, q: str = "") -> list:
    """mode: all | notified | favorites | hidden | rejected; q — поиск по названию/описанию."""
    base = WEB_ITEM_SELECT
    conds, vals = [], []
    if chat_id is not None:
        conds.append("s.chat_id = ?")
        vals.append(chat_id)
    if search_id is not None:
        conds.append("i.search_id = ?")
        vals.append(search_id)
    # поиск по лотам — в Python: LIKE в SQLite не приводит к нижнему регистру кириллицу
    if mode == "notified":
        conds.append("i.notified = 1 AND i.hidden = 0")
    elif mode == "favorites":
        conds.append("i.favorite = 1")
    elif mode == "hidden":
        conds.append("i.hidden = 1")
    elif mode == "rejected":
        conds.append("i.hidden = 0 AND (i.status LIKE 'REJECTED%' OR i.status = 'SCREENED_NO' OR i.verdict = 'REJECT')")
    else:
        conds.append("i.hidden = 0")
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    conn = _conn()
    rows = conn.execute(f"{base}{where} ORDER BY i.created_at DESC LIMIT 300", vals).fetchall()
    conn.close()
    out = [dict(r) for r in rows]
    if q:
        needle = q.strip().casefold()
        out = [
            r for r in out
            if needle in f'{r["title"]}\n{r.get("snippet") or ""}\n{r.get("reason") or ""}\n{r.get("search_title") or ""}'.casefold()
        ]
    return out


def list_searches_web(chat_id: int = None) -> list:
    conds, vals = [], []
    if chat_id is not None:
        conds.append("s.chat_id = ?")
        vals.append(chat_id)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    conn = _conn()
    rows = conn.execute(f"""
        SELECT s.id, s.chat_id, s.title, s.status, s.profile, s.created_at, s.last_scan_at,
               (SELECT COUNT(*) FROM items i WHERE i.search_id = s.id AND i.hidden = 0) AS items_count,
               (SELECT COUNT(*) FROM items i WHERE i.search_id = s.id AND i.notified = 1 AND i.hidden = 0) AS matched_count
        FROM searches s {where} ORDER BY s.id DESC LIMIT 50
    """, vals).fetchall()
    conn.close()
    out = []
    import json as _json
    for r in rows:
        d = dict(r)
        try:
            d["profile"] = _json.loads(d["profile"] or "{}")
        except Exception:
            d["profile"] = {}
        out.append(d)
    return out


def get_search_for_user(sid: int, chat_id: int) -> dict | None:
    s = get_search(sid)
    if not s or s["chat_id"] != chat_id:
        return None
    return s


def get_item_full(sid: int, avito_id: str) -> dict | None:
    conn = _conn()
    row = conn.execute(
        WEB_ITEM_SELECT + " WHERE i.search_id = ? AND i.avito_id = ?",
        (sid, str(avito_id)),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def save_item_card(sid: int, avito_id: str, info: dict):
    """Кэш карточки Авито: описание, характеристики, фото."""
    conn = _conn()
    conn.execute(
        "UPDATE items SET card_json = ? WHERE search_id = ? AND avito_id = ?",
        (json.dumps(info, ensure_ascii=False), sid, str(avito_id)),
    )
    conn.commit()
    conn.close()


def get_item_card(sid: int, avito_id: str) -> dict | None:
    """Распакованный кэш карточки (или None, если ещё не снята)."""
    conn = _conn()
    row = conn.execute(
        "SELECT card_json FROM items WHERE search_id = ? AND avito_id = ?",
        (sid, str(avito_id)),
    ).fetchone()
    conn.close()
    if not row or not row["card_json"]:
        return None
    try:
        return json.loads(row["card_json"])
    except Exception:
        return None


# ---------- коды входа на сайт ----------

def create_auth_code(chat_id: int, ttl_minutes: int = 10) -> str:
    import random
    code = f"{random.randint(0, 999999):06d}"
    conn = _conn()
    conn.execute("DELETE FROM auth_codes WHERE chat_id = ?", (chat_id,))
    conn.execute(
        "INSERT INTO auth_codes (chat_id, code, expires_at) VALUES (?, ?, datetime('now', ?))",
        (chat_id, code, f"+{ttl_minutes} minutes"),
    )
    conn.commit()
    conn.close()
    return code


def consume_auth_code(code: str) -> int | None:
    """Проверяет код входа; код одноразовый."""
    conn = _conn()
    row = conn.execute(
        "SELECT chat_id FROM auth_codes WHERE code = ? AND expires_at > datetime('now')",
        (str(code).strip(),),
    ).fetchone()
    chat_id = row["chat_id"] if row else None
    if chat_id is not None:
        conn.execute("DELETE FROM auth_codes WHERE code = ?", (str(code).strip(),))
        conn.commit()
    conn.close()
    return chat_id
