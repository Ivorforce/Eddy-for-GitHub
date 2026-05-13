"""GitHub auth: prefer GITHUB_TOKEN env var, else `gh auth token`."""
from __future__ import annotations

import logging
import os
import subprocess
import sys

import requests

log = logging.getLogger(__name__)


def get_token() -> str:
    if t := os.environ.get("GITHUB_TOKEN"):
        return t.strip()
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError:
        sys.exit("gh CLI not found. Install it: https://cli.github.com/")
    except subprocess.CalledProcessError as e:
        msg = e.stderr.strip() or "gh auth token failed"
        sys.exit(f"{msg}\nRun: gh auth login")
    token = result.stdout.strip()
    if not token:
        sys.exit("gh auth token returned empty. Run: gh auth login")
    return token


def check_scope(token: str) -> None:
    """Verify the token has the `notifications` scope by hitting the API."""
    r = requests.get(
        "https://api.github.com/notifications",
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        },
        params={"per_page": 1},
        timeout=10,
    )
    if r.status_code in (401, 403):
        sys.exit(
            "Token lacks the 'notifications' scope. "
            "Run: gh auth refresh -s notifications"
        )
    r.raise_for_status()


def fetch_identity(token: str) -> tuple[str | None, set[tuple[str, str]]]:
    """Return (login, set of (org_login, team_slug)) for the authenticated user.

    Failures are logged and yield empty results — the app still runs, just
    without 'review:you' / 'review:team' derivation.
    """
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    login: str | None = None
    teams: set[tuple[str, str]] = set()
    try:
        r = requests.get("https://api.github.com/user", headers=headers, timeout=10)
        r.raise_for_status()
        login = r.json().get("login")
    except Exception:
        log.exception("identity: failed to fetch /user")
        return None, set()

    url: str | None = "https://api.github.com/user/teams?per_page=100"
    try:
        while url:
            r = requests.get(url, headers=headers, timeout=15)
            if r.status_code == 403:
                log.warning(
                    "identity: token lacks read:org scope — skipping team membership; "
                    "team-level review notifications won't be highlighted. "
                    "Run: gh auth refresh -s read:org"
                )
                break
            r.raise_for_status()
            for t in r.json():
                org = (t.get("organization") or {}).get("login") or ""
                slug = t.get("slug") or ""
                if org and slug:
                    teams.add((org, slug))
            url = r.links.get("next", {}).get("url")
    except Exception:
        log.exception("identity: failed to fetch /user/teams")

    return login, teams
