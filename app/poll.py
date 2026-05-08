"""Background poll loop."""
from __future__ import annotations

import logging
import threading

from . import db, github

log = logging.getLogger(__name__)

DEFAULT_INTERVAL = 300  # 5 minutes


def run_loop(stop: threading.Event, token: str, interval: int = DEFAULT_INTERVAL) -> None:
    """Poll until `stop` is set. Failures are logged; the loop continues."""
    while True:
        try:
            conn = db.connect()
            try:
                n = github.poll_once(conn, token)
                if n >= 0:
                    log.info("poll: %d notifications", n)
            finally:
                conn.close()
        except Exception:
            log.exception("poll iteration failed")
        if stop.wait(interval):
            return
