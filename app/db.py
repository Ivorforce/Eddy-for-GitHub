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
# it drives the pill / priority color, but no row state is
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

# Per-thread notification-kind filter ("watch granularity"). GitHub thread
# subscriptions are all-or-nothing; this lets the user mute individual kinds
# of activity on a thread (comments / code pushes / reviews / lifecycle). When
# a poll re-delivers a notification whose new activity is *entirely* of muted
# kinds, the absorb pass (github._apply_mute_filter) re-applies the thread's
# prior state and folds the activity into the baselines.
#   muted_kinds          — JSON array of muted kind names; NULL/[] = nothing muted.
#                          Like baselines / seen_reasons, NOT touched by _apply_action.
#   baseline_head_oid    — last-seen PR head commit oid; _enrich diffs against it
#                          to recognize a "code" push. First-seen capture (like
#                          baseline_comments). PRs only.
#   effective_updated_at — local sort key. Mirrors updated_at except an absorbed
#                          delivery rolls it back to its pre-delivery value, so the
#                          row keeps its sort slot instead of jumping to the top.
SCHEMA_V25 = """
ALTER TABLE notifications ADD COLUMN muted_kinds TEXT;
ALTER TABLE notifications ADD COLUMN baseline_head_oid TEXT;
ALTER TABLE notifications ADD COLUMN effective_updated_at TEXT;
UPDATE notifications SET effective_updated_at = updated_at;
"""

# Bystander-thread throttle ("quiet bystanders"). When the meta toggle
# 'quiet_bystanders' is on (default), a burst of comment/code activity on a
# thread the user is only watching (not directed at, not authoring/commenting
# in, not tracked, not archived) surfaces once — then `throttle_until` is set
# to now+THROTTLE_WINDOW_SECONDS and further bumps inside the window get rolled
# back so the row keeps its slot. When the window expires the next activity
# bumps normally (or _release_throttled bumps once with the accumulated
# "+N new comments" behind it). Sort-layer only — not touched by _apply_action,
# never changes unread / action / baselines.
SCHEMA_V26 = """
ALTER TABLE notifications ADD COLUMN throttle_until INTEGER;
"""

# Drop the `unarchived` user_action events. A Done thread that new GitHub
# activity resurfaces no longer logs one — the activity that triggered it is
# already in the timeline and `notifications.action` going NULL is the state
# record, so the marker carried no signal (and got filtered out of every
# consumer anyway). A *user*-triggered un-archive is the `undone` event,
# which stays.
SCHEMA_V27 = """
DELETE FROM thread_events
 WHERE kind = 'user_action'
   AND json_extract(payload_json, '$.action') = 'unarchived';
"""

# Re-key synthetic search-backfill rows from "q:<node_id>" to "q_<node_id>".
# The id lands verbatim in DOM `id` attributes and from there in CSS selectors
# (`#timeline-<id>`) and custom-idents (`anchor-name: --pop-timeline-<id>`),
# where a ":" is a syntax error — the Note button's hx-target stopped resolving
# on backfilled rows, and the popover anchor positioning broke. node_ids are
# `[A-Za-z0-9_-]`, so the underscored form is a valid identifier; real thread
# ids are all-digits, so the prefix stays unambiguous. Mirrors github._is_synthetic.
SCHEMA_V28 = """
UPDATE notifications SET id        = 'q_' || substr(id, 3)        WHERE id        LIKE 'q:%';
UPDATE thread_events SET thread_id = 'q_' || substr(thread_id, 3) WHERE thread_id LIKE 'q:%';
UPDATE ai_calls      SET thread_id = 'q_' || substr(thread_id, 3) WHERE thread_id LIKE 'q:%';
"""

# Strip trailing `=` from synthetic ids. Legacy base64-padded node_ids
# (`MDU6SXNzdWU...=`) made the id end in `=`, which is invalid in a CSS
# identifier — `from:#pop-timeline-q_…=` threw "not a valid selector" from
# the timeline trigger and `anchor-name: --pop-timeline-q_…=` failed to parse,
# leaving the popover unanchored. Base64 padding is length-derived, so dropping
# it is bijective; nothing decodes a `q_*` id back to a node_id. Mirrors
# github._synth_id, which now strips at creation.
SCHEMA_V29 = r"""
UPDATE notifications SET id        = rtrim(id,        '=') WHERE id        LIKE 'q\_%=' ESCAPE '\';
UPDATE thread_events SET thread_id = rtrim(thread_id, '=') WHERE thread_id LIKE 'q\_%=' ESCAPE '\';
UPDATE ai_calls      SET thread_id = rtrim(thread_id, '=') WHERE thread_id LIKE 'q\_%=' ESCAPE '\';
"""


# action_now → disposition: `action_now` named *the user's action axis* but
# read as if the AI's verdict were a command on the AI itself ("ignore this");
# `disposition` is the legal/medical-records sense — "how the row is dealt
# with" — and matches the value set (look / queue / done / snooze / mute) as
# states the row enters, not orders to follow. Value renames sharpen the
# two-workflow framing the prompt now leans on: `ignore` → `queue` (row exits
# triage and enters the priority-sorted act-on-it queue, not "dismiss this");
# `archive` → `done` (already the user_action token and UI label — was the
# odd one out). Rewrites the cached verdict on every row plus every prior
# `ai_verdict` event the AI sees on re-judgment; `ai_calls` stays as-is —
# those are immutable audit records of the API exchange as it happened.
SCHEMA_V30 = """
UPDATE notifications
   SET ai_verdict_json = json_set(
         json_remove(ai_verdict_json, '$.action_now'),
         '$.disposition',
         CASE json_extract(ai_verdict_json, '$.action_now')
           WHEN 'ignore'  THEN 'queue'
           WHEN 'archive' THEN 'done'
           ELSE json_extract(ai_verdict_json, '$.action_now')
         END
       )
 WHERE ai_verdict_json IS NOT NULL
   AND json_extract(ai_verdict_json, '$.action_now') IS NOT NULL;

UPDATE thread_events
   SET payload_json = json_set(
         json_remove(payload_json, '$.action_now'),
         '$.disposition',
         CASE json_extract(payload_json, '$.action_now')
           WHEN 'ignore'  THEN 'queue'
           WHEN 'archive' THEN 'done'
           ELSE json_extract(payload_json, '$.action_now')
         END
       )
 WHERE kind = 'ai_verdict'
   AND json_extract(payload_json, '$.action_now') IS NOT NULL;
"""


# Identity cache for AI credibility signal. The AI already sees `login` +
# `author_association` per comment/review, but it can't tell a 2-week-old
# account from a 10-year veteran or spot a repo-org maintainer hidden among
# strangers. Extends `people` (which already exists for avatar/note/tracked)
# with cached profile fields refreshed on a 7d TTL — `fetched_at` is wall-
# clock anchored, distinct from `last_seen_at` ("when did we last see this
# login in a thread"). Adds `org_memberships` keyed on (login, org) for the
# per-repo-owner role + team slugs, which the user-global block can't carry.
# See github.fetch_user_profile / ensure_user_fresh / ensure_org_membership_fresh.
SCHEMA_V31 = """
ALTER TABLE people ADD COLUMN fetched_at INTEGER;
ALTER TABLE people ADD COLUMN bio TEXT;
ALTER TABLE people ADD COLUMN company TEXT;
ALTER TABLE people ADD COLUMN account_created_at TEXT;
ALTER TABLE people ADD COLUMN followers INTEGER;
ALTER TABLE people ADD COLUMN is_bot INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS org_memberships (
    login       TEXT NOT NULL,
    org         TEXT NOT NULL,
    teams_json  TEXT,
    fetched_at  INTEGER NOT NULL,
    PRIMARY KEY (login, org)
);
"""


# AI user-triage. Generic per-login profile summary, generated by a
# separate Haiku call against public GitHub data (no Eddy preferences,
# no org context). Two-tier output: `ai_summary_tag` (2-5 words, used in
# the thread-judge prompt's involved_people block); `ai_summary_body`
# (1-sentence, for the popover). Refreshed lazily on first encounter
# and on a 90d TTL after that. See ai.triage_user / ai_user_triage_prompt.md.
# `ai_calls.kind` splits the audit log between thread-judge and user-triage
# so cap accounting + tuning queries can filter — existing rows backfill
# to 'thread_judge' (the only kind that existed before V32).
SCHEMA_V32 = """
ALTER TABLE people ADD COLUMN ai_summary_tag TEXT;
ALTER TABLE people ADD COLUMN ai_summary_body TEXT;
ALTER TABLE people ADD COLUMN ai_summary_at INTEGER;
ALTER TABLE people ADD COLUMN ai_summary_model TEXT;

ALTER TABLE ai_calls ADD COLUMN kind TEXT NOT NULL DEFAULT 'thread_judge';
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
        if version < 25:
            conn.executescript(SCHEMA_V25)
            _set_version(conn, 25)
            version = 25
        if version < 26:
            conn.executescript(SCHEMA_V26)
            _set_version(conn, 26)
            version = 26
        if version < 27:
            conn.executescript(SCHEMA_V27)
            _set_version(conn, 27)
            version = 27
        if version < 28:
            conn.executescript(SCHEMA_V28)
            _set_version(conn, 28)
            version = 28
        if version < 29:
            conn.executescript(SCHEMA_V29)
            _set_version(conn, 29)
            version = 29
        if version < 30:
            conn.executescript(SCHEMA_V30)
            _set_version(conn, 30)
            version = 30
        if version < 31:
            conn.executescript(SCHEMA_V31)
            _set_version(conn, 31)
            version = 31
        if version < 32:
            conn.executescript(SCHEMA_V32)
            _set_version(conn, 32)
            version = 32
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
