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

# Guards _seq, _last_fingerprint, and the named-message ring buffer below.
# notify_all() wakes every SSE generator currently parked in wait() — they
# each re-check their cursors and emit whatever's new.
_cond = threading.Condition()
_seq = 0
_last_fingerprint: str | None = None

# Parallel push channel for per-thread "AI is judging this row" markers.
# Used so the auto-judge batch can light up the row's pill pulse while the
# Anthropic call is in flight — equivalent to HTMX adding `.htmx-request`
# during a manual click. A monotonic `_msg_seq` and a ring buffer so a
# briefly-disconnected client can replay anything it missed on reconnect;
# the end-of-batch fingerprint bump (existing channel) clears the marker
# by replacing the row HTML, so we don't emit an explicit "done" event.
_MSG_RING = 256
_msg_seq = 0
_msgs: list[tuple[int, str, str]] = []  # (seq, kind, thread_id)


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


def notify_judging(thread_id: str) -> None:
    """Push a per-thread `judging` marker out the SSE channel. Fired at the
    start of every AI judgment (manual click or auto-batch) so the row's
    pill can animate while the API call is in flight. No matching "done"
    event — the fingerprint bump that lands the verdict replaces the row
    HTML, dropping the class. Multiple emissions for the same thread are
    safe; the client just re-applies the same class."""
    global _msg_seq
    with _cond:
        _msg_seq += 1
        _msgs.append((_msg_seq, "judging", thread_id))
        if len(_msgs) > _MSG_RING:
            del _msgs[: len(_msgs) - _MSG_RING]
        _cond.notify_all()


def current_msg_seq() -> int:
    with _cond:
        return _msg_seq


def drain_msgs_since(last_seen: int) -> list[tuple[int, str, str]]:
    """Snapshot of buffered messages newer than `last_seen`. Empty list when
    the caller is caught up or the buffer has wrapped past their cursor."""
    with _cond:
        return [m for m in _msgs if m[0] > last_seen]


def wait_for_any(last_seq: int, last_msg_seq: int, timeout: float) -> tuple[int, int]:
    """Block until either channel advances or `timeout` elapses; return both
    cursors. Used by the SSE generator to multiplex the seq channel
    (full-table refresh trigger) with the named-message channel (per-row
    judging markers)."""
    with _cond:
        if _seq > last_seq or _msg_seq > last_msg_seq:
            return _seq, _msg_seq
        _cond.wait(timeout=timeout)
        return _seq, _msg_seq
