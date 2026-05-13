"""GitHub auth: env var → stored token → device flow."""
from __future__ import annotations

import logging
import os

import requests

from . import oauth

log = logging.getLogger(__name__)


def get_token() -> str:
    """Resolve a GitHub token. Order: GITHUB_TOKEN env → data/auth.json →
    interactive device flow (which then writes data/auth.json)."""
    if t := os.environ.get("GITHUB_TOKEN"):
        return t.strip()
    if token := oauth.load_stored_token():
        return token
    token = oauth.device_flow()
    oauth.save_token(token)
    return token


def check_scope(token: str) -> bool:
    """Probe the API to confirm `notifications` scope is granted. Returns
    True on success, False on 401/403 so the caller can re-auth."""
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
        return False
    r.raise_for_status()
    return True


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
                    "identity: /user/teams returned 403 — team membership "
                    "unavailable. Your org may restrict OAuth Apps; "
                    "team-level review notifications won't be highlighted."
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
