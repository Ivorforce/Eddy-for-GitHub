"""GitHub OAuth device flow — first-launch authorization without `gh`.

Lives at startup, behind `auth.get_token`. On a fresh install with no
`GITHUB_TOKEN` env var, prints a user code, opens github.com/login/device,
and polls until the user approves. The resulting token is written to
`data/auth.json` (mode 0600) and reused on subsequent launches.

Token storage rationale: `config/` is user-edited TOML (settings UI rewrites
it without preserving comments — wrong for a secret). `.env` is user-managed
input (writing back to it from app code inverts the convention and risks
clobbering hand comments). `data/` is gitignored, app-owned runtime state
that already houses the SQLite cache — same threat model as `gh`'s
`~/.config/gh/hosts.yml`.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import webbrowser
from pathlib import Path

import requests

log = logging.getLogger(__name__)

# Register an OAuth App at https://github.com/settings/developers — tick
# "Enable Device Flow" — and paste the Client ID here. No client secret
# required for device flow. `EDDY_OAUTH_CLIENT_ID` env overrides this for
# forks or self-hosted variants.
_PLACEHOLDER_CLIENT_ID = "Ov23liREPLACE_ME"
_DEFAULT_CLIENT_ID = "Ov23liUAnoZLM37RMOkR"

SCOPES = "notifications,read:org,read:project"
TOKEN_PATH = Path("data") / "auth.json"

_DEVICE_CODE_URL = "https://github.com/login/device/code"
_ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"


def _client_id() -> str:
    cid = (os.environ.get("EDDY_OAUTH_CLIENT_ID") or _DEFAULT_CLIENT_ID).strip()
    if cid == _PLACEHOLDER_CLIENT_ID:
        sys.exit(
            "OAuth Client ID not configured. Register an OAuth App at "
            "https://github.com/settings/developers (tick 'Enable Device "
            "Flow'), then either set EDDY_OAUTH_CLIENT_ID in your env or "
            "edit _DEFAULT_CLIENT_ID in app/oauth.py."
        )
    return cid


def load_stored_token() -> str | None:
    try:
        data = json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        log.exception("auth: failed to load %s — re-authorizing", TOKEN_PATH)
        return None
    token = data.get("token")
    if not (isinstance(token, str) and token):
        return None
    # SCOPES has grown since the stored token was minted → drop it so the
    # next get_token() runs the device flow with the current scope set.
    # Without this, the token keeps working for everything except the newly
    # added scope, which fails silently (e.g. read:project nukes the whole
    # GraphQL response for any thread carrying a project item).
    required = {s.strip() for s in SCOPES.split(",") if s.strip()}
    stored = set(data.get("scopes") or [])
    if missing := required - stored:
        log.warning(
            "auth: stored token missing scopes %s — re-authorizing",
            sorted(missing),
        )
        return None
    return token


def save_token(token: str, *, login: str | None = None) -> None:
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "token": token,
        "scopes": [s.strip() for s in SCOPES.split(",") if s.strip()],
        "login": login,
        "obtained_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    tmp = TOKEN_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass  # Best-effort; Windows / unusual FS may not support chmod.
    os.replace(tmp, TOKEN_PATH)


def clear_stored_token() -> None:
    try:
        TOKEN_PATH.unlink()
    except FileNotFoundError:
        pass


def device_flow() -> str:
    """Interactive: print user code, open browser, poll for approval."""
    cid = _client_id()
    r = requests.post(
        _DEVICE_CODE_URL,
        data={"client_id": cid, "scope": SCOPES},
        headers={"Accept": "application/json"},
        timeout=15,
    )
    r.raise_for_status()
    body = r.json()
    device_code = body["device_code"]
    user_code = body["user_code"]
    verification_uri = body["verification_uri"]
    verification_uri_complete = body.get("verification_uri_complete") or verification_uri
    interval = int(body.get("interval", 5))
    expires_in = int(body.get("expires_in", 900))

    print()
    print(f"  Authorize Eddy at: {verification_uri}")
    print(f"  Code: {user_code}")
    print("  (Opening your browser…)")
    print()
    try:
        webbrowser.open(verification_uri_complete)
    except Exception:
        pass  # Best-effort — the terminal still shows the URL and code.

    deadline = time.monotonic() + expires_in
    while time.monotonic() < deadline:
        time.sleep(interval)
        r = requests.post(
            _ACCESS_TOKEN_URL,
            data={
                "client_id": cid,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            headers={"Accept": "application/json"},
            timeout=15,
        )
        r.raise_for_status()
        body = r.json()
        if token := body.get("access_token"):
            print("  Authorized.")
            return token
        err = body.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        if err == "expired_token":
            sys.exit("Authorization code expired. Run the app again.")
        if err == "access_denied":
            sys.exit("Authorization denied.")
        sys.exit(f"OAuth error: {err or body!r}")
    sys.exit("Authorization timed out. Run the app again.")
