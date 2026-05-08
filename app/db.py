"""SQLite connection + schema migrations."""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path("data") / "notifications.db"

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS notifications (
    id           TEXT PRIMARY KEY,
    repo         TEXT NOT NULL,
    type         TEXT NOT NULL,
    title        TEXT NOT NULL,
    reason       TEXT NOT NULL,
    api_url      TEXT,
    html_url     TEXT,
    updated_at   TEXT NOT NULL,
    last_read_at TEXT,
    unread       INTEGER NOT NULL,
    raw_json     TEXT NOT NULL,
    fetched_at   INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notifications_updated_at
    ON notifications(updated_at);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

SCHEMA_V2 = """
ALTER TABLE notifications ADD COLUMN action TEXT;
ALTER TABLE notifications ADD COLUMN actioned_at INTEGER;
ALTER TABLE notifications ADD COLUMN action_source TEXT;
"""

SCHEMA_V3 = """
ALTER TABLE notifications ADD COLUMN ignored INTEGER NOT NULL DEFAULT 0;
"""

SCHEMA_V4 = """
ALTER TABLE notifications ADD COLUMN details_json TEXT;
ALTER TABLE notifications ADD COLUMN details_fetched_at INTEGER;
"""

SCHEMA_V5 = """
ALTER TABLE notifications ADD COLUMN seen_reasons TEXT;
"""

SCHEMA_V6 = """
ALTER TABLE notifications ADD COLUMN baseline_comments INTEGER;
"""

SCHEMA_V7 = """
ALTER TABLE notifications ADD COLUMN pr_reactions INTEGER;
ALTER TABLE notifications ADD COLUMN pr_reactions_fetched_at INTEGER;
"""

SCHEMA_V8 = """
ALTER TABLE notifications ADD COLUMN pr_reactions_json TEXT;
"""

SCHEMA_V9 = """
ALTER TABLE notifications ADD COLUMN unique_commenters INTEGER;
"""

SCHEMA_V10 = """
ALTER TABLE notifications ADD COLUMN pr_review_state TEXT;
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init() -> None:
    conn = connect()
    try:
        version = _get_version(conn)
        if version < 1:
            conn.executescript(SCHEMA_V1)
            _set_version(conn, 1)
            version = 1
        if version < 2:
            conn.executescript(SCHEMA_V2)
            _set_version(conn, 2)
            version = 2
        if version < 3:
            conn.executescript(SCHEMA_V3)
            _set_version(conn, 3)
            version = 3
        if version < 4:
            conn.executescript(SCHEMA_V4)
            _set_version(conn, 4)
            version = 4
        if version < 5:
            conn.executescript(SCHEMA_V5)
            _set_version(conn, 5)
            version = 5
        if version < 6:
            conn.executescript(SCHEMA_V6)
            _set_version(conn, 6)
            version = 6
        if version < 7:
            conn.executescript(SCHEMA_V7)
            _set_version(conn, 7)
            version = 7
        if version < 8:
            conn.executescript(SCHEMA_V8)
            _set_version(conn, 8)
            version = 8
        if version < 9:
            conn.executescript(SCHEMA_V9)
            _set_version(conn, 9)
            version = 9
        if version < 10:
            conn.executescript(SCHEMA_V10)
            _set_version(conn, 10)
            version = 10
    finally:
        conn.close()


def _get_version(conn: sqlite3.Connection) -> int:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
    )
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()
    return int(row["value"]) if row else 0


def _set_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(version),),
    )


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    if value is None:
        conn.execute("DELETE FROM meta WHERE key = ?", (key,))
        return
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
