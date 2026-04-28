"""
SQLite layer — schema creation and thin helpers.
All callers import `get_db()` which returns the connection for the current process.
"""

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path("data.db")

_local = threading.local()


def get_db() -> sqlite3.Connection:
    """Return a per-thread connection, creating it on first use."""
    if not hasattr(_local, "conn"):
        _local.conn = _connect()
    return _local.conn


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _create_schema(conn)
    return conn


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS events (
        id          TEXT PRIMARY KEY,
        source      TEXT NOT NULL,
        course_code TEXT,
        event_type  TEXT,
        title       TEXT,
        body        TEXT,
        occurs_at   TIMESTAMP,
        fetched_at  TIMESTAMP,
        raw_json    TEXT
    );

    CREATE TABLE IF NOT EXISTS documents (
        id           TEXT PRIMARY KEY,
        source       TEXT,
        course_code  TEXT,
        doc_type     TEXT,
        title        TEXT,
        url          TEXT,
        local_path   TEXT,
        content_hash TEXT,
        fetched_at   TIMESTAMP,
        processed_at TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS actions_taken (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        ts           TIMESTAMP NOT NULL,
        action_type  TEXT NOT NULL,
        external_id  TEXT UNIQUE,
        payload      TEXT,
        user_response TEXT,
        responded_at  TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS llm_cache (
        prompt_hash TEXT PRIMARY KEY,
        response    TEXT NOT NULL,
        created_at  TIMESTAMP NOT NULL
    );

    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def cache_get(prompt_hash: str) -> str | None:
    row = get_db().execute(
        "SELECT response FROM llm_cache WHERE prompt_hash = ?", (prompt_hash,)
    ).fetchone()
    return row["response"] if row else None


def cache_set(prompt_hash: str, response: str) -> None:
    get_db().execute(
        "INSERT OR REPLACE INTO llm_cache (prompt_hash, response, created_at) VALUES (?, ?, ?)",
        (prompt_hash, response, now_iso()),
    )
    get_db().commit()


def hash_prompt(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def log_action(
    action_type: str,
    external_id: str | None,
    payload: dict,
) -> int:
    """Insert into actions_taken. Returns row id. Skips if external_id already exists."""
    if external_id:
        row = get_db().execute(
            "SELECT id FROM actions_taken WHERE external_id = ?", (external_id,)
        ).fetchone()
        if row:
            return row["id"]

    cur = get_db().execute(
        "INSERT INTO actions_taken (ts, action_type, external_id, payload) VALUES (?, ?, ?, ?)",
        (now_iso(), action_type, external_id, json.dumps(payload)),
    )
    get_db().commit()
    return cur.lastrowid


def mark_completed(external_id: str) -> bool:
    """Set user_response='completed' for a given external_id. Returns True if found."""
    cur = get_db().execute(
        "UPDATE actions_taken SET user_response='completed', responded_at=? WHERE external_id=?",
        (now_iso(), external_id),
    )
    get_db().commit()
    return cur.rowcount > 0


def get_open_actions(action_type: str | None = None) -> list[sqlite3.Row]:
    q = "SELECT * FROM actions_taken WHERE user_response IS NULL"
    params: list = []
    if action_type:
        q += " AND action_type = ?"
        params.append(action_type)
    return get_db().execute(q, params).fetchall()


def meta_get(key: str) -> str | None:
    row = get_db().execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def meta_set(key: str, value: str) -> None:
    get_db().execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
    get_db().commit()


def upsert_event(
    id: str,
    source: str,
    course_code: str | None,
    event_type: str | None,
    title: str,
    body: str | None = None,
    occurs_at: str | None = None,
    raw_json: str | None = None,
) -> None:
    get_db().execute(
        """INSERT OR REPLACE INTO events
           (id, source, course_code, event_type, title, body, occurs_at, fetched_at, raw_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (id, source, course_code, event_type, title, body, occurs_at, now_iso(), raw_json),
    )
    get_db().commit()


def upsert_document(
    id: str,
    source: str,
    course_code: str | None,
    doc_type: str | None,
    title: str,
    local_path: str | None = None,
    url: str | None = None,
    content_hash: str | None = None,
) -> None:
    get_db().execute(
        """INSERT OR REPLACE INTO documents
           (id, source, course_code, doc_type, title, url, local_path, content_hash, fetched_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (id, source, course_code, doc_type, title, url, local_path, content_hash, now_iso()),
    )
    get_db().commit()
