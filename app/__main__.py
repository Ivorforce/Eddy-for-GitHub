"""Entry point: `python -m app run`."""
from __future__ import annotations

import logging
import os
import sys
import threading

from dotenv import load_dotenv

from . import auth, db, poll, web


def main() -> int:
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    db.init()
    token = auth.get_token()
    auth.check_scope(token)

    user_login, user_teams = auth.fetch_identity(token)
    if user_login:
        logging.getLogger(__name__).info(
            "identity: %s, %d team(s)", user_login, len(user_teams)
        )

    web.app.config["GITHUB_TOKEN"] = token
    web.app.config["USER_LOGIN"] = user_login
    web.app.config["USER_TEAMS"] = user_teams
    # Hot-reload Jinja templates on edit. Both the config flag and the env
    # attribute are required: web.py touches app.jinja_env at import (to
    # register the humanize filter), which constructs the env with the config
    # value at that moment (False) and caches it. Setting only the env
    # attribute later got reset by Flask somewhere between import and the
    # first request — setting the config too keeps it sticky. Full Flask
    # debug stays off because its reloader respawns the process and would
    # double the poll thread.
    web.app.config["TEMPLATES_AUTO_RELOAD"] = True
    web.app.jinja_env.auto_reload = True

    stop = threading.Event()
    poller = threading.Thread(
        target=poll.run_loop,
        args=(stop, token),
        daemon=True,
        name="poller",
    )
    poller.start()

    port = int(os.environ.get("PORT", "5734"))
    print(f"Serving on http://127.0.0.1:{port}", flush=True)
    try:
        web.app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
    finally:
        stop.set()
    return 0


if __name__ == "__main__":
    # Accept and ignore subcommand args (reserved: `run`).
    args = sys.argv[1:]
    if args and args[0] not in ("run",):
        print(f"unknown subcommand: {args[0]}", file=sys.stderr)
        sys.exit(2)
    sys.exit(main())
