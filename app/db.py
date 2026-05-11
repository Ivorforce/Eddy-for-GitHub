"""SQLite connection + schema migrations."""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime
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

SCHEMA_V11 = """
ALTER TABLE notifications ADD COLUMN baseline_review_state TEXT;
"""

SCHEMA_V12 = """
ALTER TABLE notifications ADD COLUMN note_user TEXT;
ALTER TABLE notifications ADD COLUMN note_ai TEXT;
ALTER TABLE notifications ADD COLUMN is_favorite INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS people (
    login              TEXT PRIMARY KEY,
    avatar_url         TEXT,
    note_user          TEXT,
    note_ai            TEXT,
    is_favorite        INTEGER NOT NULL DEFAULT 0,
    last_seen_at       INTEGER
);
"""

SCHEMA_V13 = """
CREATE TABLE IF NOT EXISTS repos (
    name         TEXT PRIMARY KEY,
    note_user    TEXT,
    note_ai      TEXT,
    is_favorite  INTEGER NOT NULL DEFAULT 0,
    last_seen_at INTEGER
);

CREATE TABLE IF NOT EXISTS orgs (
    name         TEXT PRIMARY KEY,
    note_user    TEXT,
    note_ai      TEXT,
    is_favorite  INTEGER NOT NULL DEFAULT 0,
    last_seen_at INTEGER
);
"""

# Rename is_favorite → is_tracked across all four tables. The user-facing
# concept shifted from "favorite" (positive) to "tracked" (neutral) so the
# DB names follow.
SCHEMA_V14 = """
ALTER TABLE notifications RENAME COLUMN is_favorite TO is_tracked;
ALTER TABLE people        RENAME COLUMN is_favorite TO is_tracked;
ALTER TABLE repos         RENAME COLUMN is_favorite TO is_tracked;
ALTER TABLE orgs          RENAME COLUMN is_favorite TO is_tracked;
"""

SCHEMA_V15 = """
ALTER TABLE notifications ADD COLUMN unique_reviewers INTEGER;
"""

# pr_reactions_fetched_at became redundant when PR enrichment moved to a
# single GraphQL query: reactions now arrive with details, so the column
# always equalled details_fetched_at. Drop it.
SCHEMA_V16 = """
ALTER TABLE notifications DROP COLUMN pr_reactions_fetched_at;
"""

# link_url: per-event browser URL derived from subject.latest_comment_url.
# Updated whenever a poll surfaces a new latest_comment_url, otherwise
# preserved across read/unread transitions. Lets the title link land on the
# latest event instead of scrolling the user back to the top of a long thread.
SCHEMA_V17 = """
ALTER TABLE notifications ADD COLUMN link_url TEXT;
"""

# AI verdict cache columns. ai_verdict_json holds the structured tool-call
# output from judge_thread; ai_verdict_at is when it was produced; model is
# the model ID that produced it (so the UI can flag stale/legacy verdicts
# and the user can audit which model said what). The verdict is advisory:
# it drives the pill / signals / priority color, but no row state is
# auto-applied. Re-ask overwrites it; nothing else clears it.
SCHEMA_V18 = """
ALTER TABLE notifications ADD COLUMN ai_verdict_json TEXT;
ALTER TABLE notifications ADD COLUMN ai_verdict_at INTEGER;
ALTER TABLE notifications ADD COLUMN ai_verdict_model TEXT;
"""

# Append-only log of every Anthropic call, including ones blocked by the
# soft daily cap or aborted by an exception. Enough detail to tune the
# prompt later without re-asking the model: full request + response,
# token breakdown (so cache-hit ratio is visible), estimated cost.
SCHEMA_V19 = """
CREATE TABLE IF NOT EXISTS ai_calls (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id             TEXT NOT NULL,
    created_at            INTEGER NOT NULL,
    model                 TEXT NOT NULL,
    request_json          TEXT NOT NULL,
    response_json         TEXT,
    input_tokens          INTEGER,
    cache_read_tokens     INTEGER,
    cache_creation_tokens INTEGER,
    output_tokens         INTEGER,
    cost_usd              REAL,
    error                 TEXT,
    status                TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_calls_created_at ON ai_calls(created_at);
CREATE INDEX IF NOT EXISTS idx_ai_calls_thread     ON ai_calls(thread_id);
"""

# Per-thread chronological event log — the substrate for the stateful AI
# integration. Captures GitHub events (comments, reviews), AI verdicts,
# user actions on those verdicts, and free-text user chat directed at the
# AI on a specific thread.
#
# Columns:
#   ts          — event time (GitHub createdAt / submittedAt for fetched
#                 items, or local clock for user/AI events). Drives the
#                 timeline ordering shown to both AI and user.
#   inserted_at — when WE wrote the row. Distinct from ts because GitHub
#                 events get backdated to their createdAt.
#   kind        — 'comment' | 'review' | 'lifecycle' | 'ai_verdict' |
#                 'user_action' | 'user_chat' | 'priority_change' (room to
#                 grow: 'ai_recap', 'static_changed').
#   source      — 'github' | 'user' | 'ai'.
#   external_id — stable dedup key when meaningful: GitHub comment / review
#                 databaseId, ai_calls.id. NULL for user_action / user_chat
#                 (each click/message is its own event).
#   payload_json — kind-specific JSON body (author, body, state, etc.).
#
# The unique partial index makes re-fetch idempotent: same external_id =
# UPDATE the payload (in case the comment body was edited), don't append.
SCHEMA_V20 = """
CREATE TABLE IF NOT EXISTS thread_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id    TEXT NOT NULL,
    ts           INTEGER NOT NULL,
    inserted_at  INTEGER NOT NULL,
    kind         TEXT NOT NULL,
    source       TEXT NOT NULL,
    external_id  TEXT,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_thread_events_thread_ts
    ON thread_events(thread_id, ts);
CREATE UNIQUE INDEX IF NOT EXISTS idx_thread_events_external
    ON thread_events(thread_id, kind, external_id)
    WHERE external_id IS NOT NULL;
"""

# AI verdicts are advisory-only — they shape display, but no row state is
# auto-applied on the user's behalf. The note_ai column existed to persist
# the verdict description after an "approve" click; with approve/dismiss
# gone, the description lives on the cached verdict + ai_verdict timeline
# events and there's nothing to write to note_ai. Drop it on all four
# entity tables.
SCHEMA_V21 = """
ALTER TABLE notifications DROP COLUMN note_ai;
ALTER TABLE people        DROP COLUMN note_ai;
ALTER TABLE repos         DROP COLUMN note_ai;
ALTER TABLE orgs          DROP COLUMN note_ai;
"""

# User-set priority (superseded by V23, which switches the column to REAL).
SCHEMA_V22 = """
ALTER TABLE notifications ADD COLUMN priority_user TEXT;
"""

# Store priority_user as a 0–1 float, not a band name: priorities (user-set
# and AI) are floats end-to-end, so the named bands ("normal", "high", …)
# stay a display layer whose boundaries can be re-tuned without migrating
# data. NULL = unset ("auto" — fall back to the AI verdict's score). The
# user owns the displayed priority until the next AI verdict, which clears
# this column (the choice survives as a priority_change timeline event the
# next judgment reads as calibration). Column is empty in practice (shipped
# in the same batch), so a plain drop/re-add is safe.
SCHEMA_V23 = """
ALTER TABLE notifications DROP COLUMN priority_user;
ALTER TABLE notifications ADD COLUMN priority_user REAL;
"""

# Snooze: a unix ts at which a deferred ("done for now") thread should
# resurface. While set, the row carries action='snoozed' (hidden by default,
# like 'done'); the poll loop wakes it when the ts passes (action→'woken',
# unread=1), and a new-activity resurface clears it too. NULL = not snoozed.
SCHEMA_V24 = """
ALTER TABLE notifications ADD COLUMN snooze_until INTEGER;
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
        if version < 11:
            conn.executescript(SCHEMA_V11)
            _set_version(conn, 11)
            version = 11
        if version < 12:
            conn.executescript(SCHEMA_V12)
            _set_version(conn, 12)
            version = 12
        if version < 13:
            conn.executescript(SCHEMA_V13)
            _set_version(conn, 13)
            version = 13
        if version < 14:
            conn.executescript(SCHEMA_V14)
            _set_version(conn, 14)
            version = 14
        if version < 15:
            conn.executescript(SCHEMA_V15)
            _set_version(conn, 15)
            version = 15
        if version < 16:
            conn.executescript(SCHEMA_V16)
            _set_version(conn, 16)
            version = 16
        if version < 17:
            conn.executescript(SCHEMA_V17)
            _set_version(conn, 17)
            version = 17
        if version < 18:
            conn.executescript(SCHEMA_V18)
            _set_version(conn, 18)
            version = 18
        if version < 19:
            conn.executescript(SCHEMA_V19)
            _set_version(conn, 19)
            version = 19
        if version < 20:
            conn.executescript(SCHEMA_V20)
            _set_version(conn, 20)
            version = 20
        if version < 21:
            conn.executescript(SCHEMA_V21)
            _set_version(conn, 21)
            version = 21
        if version < 22:
            conn.executescript(SCHEMA_V22)
            _set_version(conn, 22)
            version = 22
        if version < 23:
            conn.executescript(SCHEMA_V23)
            _set_version(conn, 23)
            version = 23
        if version < 24:
            conn.executescript(SCHEMA_V24)
            _set_version(conn, 24)
            version = 24
    finally:
        conn.close()


# ---- thread_events helpers ----------------------------------------------

def iso_to_unix(s: str | None) -> int | None:
    """GraphQL ISO timestamp → unix int. Used to map createdAt/submittedAt
    to the `ts` column on thread_events so the timeline orders by actual
    GitHub event time, not local fetch time."""
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return None


def write_thread_event(
    conn: sqlite3.Connection,
    *,
    thread_id: str,
    ts: int,
    kind: str,
    source: str,
    payload: dict,
    external_id: str | None = None,
) -> None:
    """Write one row to thread_events. Idempotent for events with an
    external_id (re-fetched comments / reviews update their payload in
    place rather than appending duplicates). Always-insert for events
    without one — every user_action / user_chat is its own event.

    The partial unique index on (thread_id, kind, external_id) WHERE
    external_id IS NOT NULL is what makes the upsert work: rows with
    NULL external_id are not indexed, so the conflict clause never fires
    on them and the insert proceeds cleanly."""
    conn.execute(
        """
        INSERT INTO thread_events
            (thread_id, ts, inserted_at, kind, source, external_id, payload_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (thread_id, kind, external_id) WHERE external_id IS NOT NULL
        DO UPDATE SET payload_json = excluded.payload_json, ts = excluded.ts
        """,
        (
            thread_id,
            ts,
            int(time.time()),
            kind,
            source,
            external_id,
            json.dumps(payload, ensure_ascii=False),
        ),
    )


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
