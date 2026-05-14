"""Background poll loop."""
from __future__ import annotations

import logging
import threading
import time

from . import db, events, github, settings

log = logging.getLogger(__name__)

# Per-iteration wake. The loop re-reads the auto_refresh cadence and the
# last_poll_at stamp each tick, so this is the granularity of: how soon a
# cadence change takes effect, how close to a wall-clock boundary the fetch
# fires, and how promptly snooze timers wake. 60s is a fine balance — small
# enough for honest "next at HH:00" punctuality, large enough that the loop
# is effectively idle (one meta read + a snooze-table scan per minute).
WAKE_INTERVAL = 60

LIVE_INTERVAL = 300  # 5 minutes — interval-since-last for the "live" mode


def _most_recent_local_tick(now: int, hour: int, minute: int = 0) -> int:
    """Epoch second of the most recent local `hour:minute` (today's if it has
    already passed, yesterday's otherwise). Used for the wall-clock-anchored
    Hourly (last xx:00 local) and Daily (last 04:00 local) cadences. Going
    through `localtime` + `mktime` instead of epoch arithmetic so fractional-
    hour timezones (IST etc.) still hit local boundaries — matters because
    the client-side tooltip is also rendered against the user's local clock
    and they must agree."""
    lt = time.localtime(now)
    tick = int(time.mktime(
        (lt.tm_year, lt.tm_mon, lt.tm_mday, hour, minute, 0, 0, 0, -1)
    ))
    return tick if now >= tick else tick - 86400


def _most_recent_hourly_tick(now: int) -> int:
    """Most recent local xx:00. Same `mktime` trick as the daily case — see
    `_most_recent_local_tick`."""
    lt = time.localtime(now)
    tick = int(time.mktime(
        (lt.tm_year, lt.tm_mon, lt.tm_mday, lt.tm_hour, 0, 0, 0, 0, -1)
    ))
    return tick if now >= tick else tick - 3600


def _is_due(mode: str, last_poll_at: int, now: int) -> bool:
    """Should an auto-poll fire right now? See `web.AUTO_REFRESH_MODES` for
    the mode set. Hourly and Daily are wall-clock anchored so launches that
    miss a tick catch up immediately and the rhythm stays predictable;
    Live is interval-since-last; Manual never fires automatically."""
    if mode == "manual":
        return False
    if mode == "live":
        return now - last_poll_at >= LIVE_INTERVAL
    if mode == "hourly":
        return last_poll_at < _most_recent_hourly_tick(now)
    if mode == "daily":
        return last_poll_at < _most_recent_local_tick(now, hour=4)
    return False


def _wake_snoozed(conn, token: str) -> int:
    """Resurface snoozed threads whose wake time has passed: clear the snooze,
    bring them back to the inbox marked unread (a real reminder), re-subscribe
    if it was a quiet snooze (`ignored`), and log a `woken` user_action so the
    timeline records why it returned. A re-subscribe that fails leaves the row
    snoozed-with-an-expired-timer so the next poll retries. Returns the number
    of threads woken."""
    now = int(time.time())
    rows = conn.execute(
        "SELECT id, ignored FROM notifications "
        "WHERE action = 'snoozed' AND snooze_until IS NOT NULL AND snooze_until <= ?",
        (now,),
    ).fetchall()
    woke = 0
    for r in rows:
        if r["ignored"]:
            try:
                github.set_subscribed(token, r["id"])
            except Exception:
                log.warning("snooze wake: re-subscribe failed for %s; will retry", r["id"])
                continue
        conn.execute(
            "UPDATE notifications SET action = 'woken', actioned_at = ?, "
            "action_source = 'github', snooze_until = NULL, unread = 1, ignored = 0 "
            "WHERE id = ?",
            (now, r["id"]),
        )
        db.write_thread_event(
            conn, thread_id=r["id"], ts=now, kind="user_action", source="github",
            payload={"action": "woken", **({"quiet": True} if r["ignored"] else {})},
        )
        woke += 1
    return woke


def run_loop(
    stop: threading.Event,
    token: str,
    *,
    user_login: str | None = None,
    user_teams=None,
    wake_interval: int = WAKE_INTERVAL,
) -> None:
    """Poll until `stop` is set. Failures are logged; the loop continues.

    Wakes every `wake_interval` seconds, reads the persisted `auto_refresh`
    cadence and `last_poll_at` stamp, and fetches iff the next scheduled
    tick is due (see `_is_due`). The first time we actually fetch — whether
    on launch or after a long idle stretch in manual mode — runs a full
    sync (combined fetch + dedicated unread fetch); subsequent fetches let
    `poll_once`'s predicate skip the unread fetch on a quiet inbox.

    Snooze wake (`_wake_snoozed`) and the SSE fingerprint check
    (`events.notify_if_changed`) run every wake regardless of mode — snoozes
    must fire on time even in manual mode, and a fingerprint that moves
    (e.g., a snooze auto-woke a thread) needs to push to connected clients.

    `user_login` / `user_teams` are accepted for API stability — earlier the
    poll loop ran `ai.auto_judge_eligible` and needed identity; auto-judge is
    now focus-triggered, so the poll loop itself doesn't use them. Kept on
    the signature so callers (and tests) don't need to change.
    """
    force_full = True
    while True:
        try:
            conn = db.connect()
            try:
                mode = settings.get("auto_refresh")
                last_poll_at = int(db.get_meta(conn, "last_poll_at") or "0")
                now = int(time.time())
                if _is_due(mode, last_poll_at, now):
                    n = github.poll_once(conn, token, force_full=force_full)
                    if n >= 0:
                        log.info("poll: %d notifications (mode=%s)", n, mode)
                    # Stamp after the call so a long-running poll doesn't
                    # leave the next wake thinking it's due again.
                    db.set_meta(conn, "last_poll_at", str(int(time.time())))
                    conn.commit()
                    force_full = False
                woke = _wake_snoozed(conn, token)
                if woke:
                    log.info("snooze: woke %d thread(s)", woke)
                # Push the change to any connected SSE consumers. The fingerprint
                # gate inside notify_if_changed makes this cheap on a no-op poll —
                # no bump, no client refresh.
                events.notify_if_changed(conn)
                # Auto-judge no longer runs on the poll cycle — it's
                # focus-triggered via /ai/auto-judge-batch so we don't burn
                # API spend judging rows the user can't see right now.
            finally:
                conn.close()
        except Exception:
            log.exception("poll iteration failed")
        if stop.wait(wake_interval):
            return
