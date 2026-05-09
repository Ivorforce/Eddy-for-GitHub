"""Background poll loop."""
from __future__ import annotations

import logging
import threading

from . import db, github

log = logging.getLogger(__name__)

DEFAULT_INTERVAL = 300  # 5 minutes


def run_loop(stop: threading.Event, token: str, interval: int = DEFAULT_INTERVAL) -> None:
    """Poll until `stop` is set. Failures are logged; the loop continues.

    The first iteration runs a full sync (combined fetch + dedicated unread
    fetch) so app launch always reconciles read-state fully, regardless of
    what happened while the app was closed. Subsequent iterations let
    poll_once's predicate skip the unread fetch when local state has no
    items outside the latest-100 window.
    """
    force_full = True
    while True:
        try:
            conn = db.connect()
            try:
                n = github.poll_once(conn, token, force_full=force_full)
                if n >= 0:
                    log.info("poll: %d notifications", n)
            finally:
                conn.close()
        except Exception:
            log.exception("poll iteration failed")
        force_full = False
        if stop.wait(interval):
            return
