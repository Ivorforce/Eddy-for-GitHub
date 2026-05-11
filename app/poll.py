"""Background poll loop."""
from __future__ import annotations

import logging
import threading
import time

from . import db, github

log = logging.getLogger(__name__)

DEFAULT_INTERVAL = 300  # 5 minutes


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


def run_loop(stop: threading.Event, token: str, interval: int = DEFAULT_INTERVAL) -> None:
    """Poll until `stop` is set. Failures are logged; the loop continues.

    The first iteration runs a full sync (combined fetch + dedicated unread
    fetch) so app launch always reconciles read-state fully, regardless of
    what happened while the app was closed. Subsequent iterations let
    poll_once's predicate skip the unread fetch when local state has no
    items outside the latest-100 window. Each iteration also wakes any
    snoozed threads whose timer has expired.
    """
    force_full = True
    while True:
        try:
            conn = db.connect()
            try:
                n = github.poll_once(conn, token, force_full=force_full)
                if n >= 0:
                    log.info("poll: %d notifications", n)
                woke = _wake_snoozed(conn, token)
                if woke:
                    log.info("snooze: woke %d thread(s)", woke)
            finally:
                conn.close()
        except Exception:
            log.exception("poll iteration failed")
        force_full = False
        if stop.wait(interval):
            return
