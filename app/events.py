"""Server → client push channel.

A single monotonic `_seq` counter is bumped whenever a poll or refresh
materially changed user-visible state — gated on a fingerprint over every
column that feeds the row template, so silent polls don't churn the client.
SSE consumers in `web.py` wait on the counter and emit a `data: <seq>`
message when it advances; the page-level EventSource then triggers an
auto-refresh, replacing the old visibilitychange / interval poll on the
client.

The fingerprint logic also doubles for `/refresh`'s 204 short-circuit (was
previously a private helper in web.py): one canonical "did rendering inputs
change" definition for both the push signal and the response decision.
"""
from __future__ import annotations

import hashlib
import sqlite3
import threading

# Guards both _seq and _last_fingerprint. notify_all() wakes every SSE
# generator currently parked in wait() — they each re-check _seq against
# their own last_seen and emit if it's moved.
_cond = threading.Condition()
_seq = 0
_last_fingerprint: str | None = None


def compute_fingerprint(conn: sqlite3.Connection) -> str:
    """Hash of everything that feeds the row template — GitHub-side state lives
    in raw_json, the rest are local-state / enrichment columns — plus a coarse
    thread_events summary (new comments / reviews shift the pill counts). The
    rendered relative ages drift on their own; we fingerprint their inputs
    (updated_at / effective_updated_at), not the output. Being generous with
    the column list is cheap; the failure mode of omitting one is just "a real
    change pushes one poll late"."""
    h = hashlib.blake2b(digest_size=16)
    for row in conn.execute(
        "SELECT id, raw_json, updated_at, unread, action, ignored, is_tracked, "
        "snooze_until, throttle_until, muted_kinds, effective_updated_at, link_url, "
        "note_user, priority_user, ai_verdict_json, ai_verdict_at, details_json, "
        "baseline_comments, baseline_review_state, baseline_head_oid, "
        "unique_commenters, unique_reviewers, pr_review_state, pr_reactions, "
        "seen_reasons FROM notifications ORDER BY id"
    ):
        h.update(repr(tuple(row)).encode())
    h.update(repr(tuple(conn.execute(
        "SELECT COUNT(*), COALESCE(MAX(id), 0) FROM thread_events"
    ).fetchone())).encode())
    return h.hexdigest()


def notify_if_changed(conn: sqlite3.Connection) -> bool:
    """Recompute the fingerprint; if it differs from the last bump, increment
    _seq and wake every waiter. Returns True iff a bump happened, so callers
    can also use it as a "did anything change" decision (e.g. the /refresh
    handler's 204 short-circuit)."""
    global _seq, _last_fingerprint
    fp = compute_fingerprint(conn)
    with _cond:
        if fp == _last_fingerprint:
            return False
        _last_fingerprint = fp
        _seq += 1
        _cond.notify_all()
        return True


def current_seq() -> int:
    with _cond:
        return _seq


def wait(last_seen: int, timeout: float) -> int:
    """Block until _seq > last_seen or `timeout` elapses; return current _seq.
    Caller distinguishes "advanced" from "timeout" by comparing the return
    value to its prior last_seen — a wake without advance is a keepalive cue."""
    with _cond:
        if _seq > last_seen:
            return _seq
        _cond.wait(timeout=timeout)
        return _seq
