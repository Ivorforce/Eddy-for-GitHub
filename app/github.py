"""GitHub /notifications fetcher + upsert into SQLite."""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

import requests

from . import db

API_NOTIFICATIONS = "https://api.github.com/notifications"
PER_PAGE = 50
MAX_PAGES_PER_FETCH = 2  # ~100 items per fetch — bound poll cost
ENRICHMENT_PER_POLL = 20

log = logging.getLogger(__name__)


def derive_html_url(item: dict[str, Any]) -> str | None:
    """Convert subject.url (api.github.com) to a github.com browser URL."""
    subject = item.get("subject") or {}
    api_url = subject.get("url")
    repo = item.get("repository") or {}
    repo_html = repo.get("html_url")

    if not api_url or not api_url.startswith("https://api.github.com/repos/"):
        return repo_html

    path = api_url[len("https://api.github.com/repos/") :]
    # /repos/owner/repo/pulls/N  -> /owner/repo/pull/N
    path = path.replace("/pulls/", "/pull/")
    # /repos/owner/repo/releases/N has no usable html mapping (need tag);
    # fall back to the repo's releases page.
    if "/releases/" in path:
        return f"{repo_html}/releases" if repo_html else None
    return f"https://github.com/{path}"


def _auth_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _thread_url(thread_id: str) -> str:
    return f"https://api.github.com/notifications/threads/{thread_id}"


def mark_read(token: str, thread_id: str) -> None:
    """Mark a notification thread as read. Stays in the inbox."""
    r = requests.patch(_thread_url(thread_id), headers=_auth_headers(token), timeout=10)
    if r.status_code in (200, 205, 304):
        return
    r.raise_for_status()


def mark_done(token: str, thread_id: str) -> None:
    """Mark as done — clears the notification from the inbox."""
    r = requests.delete(_thread_url(thread_id), headers=_auth_headers(token), timeout=10)
    if r.status_code in (204, 404):
        return  # 404 if already gone — idempotent
    r.raise_for_status()


def set_ignored(token: str, thread_id: str) -> None:
    """Set thread subscription to ignored — stops future notifications on this thread."""
    r = requests.put(
        f"{_thread_url(thread_id)}/subscription",
        headers=_auth_headers(token),
        json={"ignored": True},
        timeout=10,
    )
    if r.status_code == 200:
        return
    r.raise_for_status()


def backfetch(conn: sqlite3.Connection, token: str, n: int = 20) -> int:
    """Temporary helper: pull the last N notifications (including read) and force
    re-enrichment of those rows. Useful while iterating on enrichment fields.
    """
    r = requests.get(
        API_NOTIFICATIONS,
        headers=_auth_headers(token),
        params={"per_page": n, "all": "true"},
        timeout=30,
    )
    r.raise_for_status()
    items = r.json()
    now = int(time.time())
    ids: list[str] = []
    for item in items:
        if not item.get("id"):
            continue
        ids.append(item["id"])
        _upsert(conn, item, now)

    # Force the next _enrich pass to re-fetch details (and PR reactions) for these.
    if ids:
        placeholders = ",".join(["?"] * len(ids))
        conn.execute(
            f"UPDATE notifications "
            f"SET details_fetched_at = NULL, pr_reactions_fetched_at = NULL "
            f"WHERE id IN ({placeholders})",
            tuple(ids),
        )
    _enrich(conn, token)
    return len(items)


def fetch_details(token: str, api_url: str | None) -> dict | None:
    """GET subject.url for a notification thread (the underlying PR or Issue payload).

    Returns parsed JSON, or None if api_url is missing or the resource is gone (404).
    """
    if not api_url:
        return None
    r = requests.get(api_url, headers=_auth_headers(token), timeout=15)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def _compute_review_state(reviews: list[dict]) -> str | None:
    """Latest non-comment review per author wins. Returns
    'changes_requested' | 'approved' | None.

    GitHub returns reviews in chronological order. COMMENTED reviews don't
    update an author's stance. DISMISSED reviews invalidate prior approvals
    or changes-requested from that author.
    """
    by_author: dict[str, str] = {}
    for r in reviews:
        author = (r.get("user") or {}).get("login")
        state = r.get("state")
        if not author or state in ("PENDING", "COMMENTED"):
            continue
        by_author[author] = state  # APPROVED, CHANGES_REQUESTED, or DISMISSED

    if any(s == "CHANGES_REQUESTED" for s in by_author.values()):
        return "changes_requested"
    if any(s == "APPROVED" for s in by_author.values()):
        return "approved"
    return None


def fetch_pr_review_state(token: str, pr_api_url: str | None) -> str | None:
    """Returns 'approved' | 'changes_requested' | None for a PR's review state."""
    if not pr_api_url or "/pulls/" not in pr_api_url:
        return None
    r = requests.get(
        pr_api_url + "/reviews",
        headers=_auth_headers(token),
        params={"per_page": 100},
        timeout=15,
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return _compute_review_state(r.json())


def fetch_unique_commenters(
    token: str, api_url: str | None, comments_count: int, max_pages: int = 5
) -> int | None:
    """Count distinct commenter logins on an Issue or PR.
    Skips the API call entirely when comments_count == 0.
    For PRs we use the issue-form comments endpoint (where regular discussion
    lives, not line-anchored review comments)."""
    if not api_url or comments_count <= 0:
        return 0
    if "/pulls/" in api_url:
        url = api_url.replace("/pulls/", "/issues/", 1) + "/comments"
    else:
        url = api_url + "/comments"
    logins: set[str] = set()
    for page in range(1, max_pages + 1):
        r = requests.get(
            url,
            headers=_auth_headers(token),
            params={"per_page": 100, "page": page},
            timeout=20,
        )
        if r.status_code == 404:
            return 0
        r.raise_for_status()
        items = r.json()
        for c in items:
            login = (c.get("user") or {}).get("login")
            if login:
                logins.add(login)
        if len(items) < 100:
            break
    return len(logins)


def fetch_pr_reactions(token: str, pr_api_url: str | None) -> dict | None:
    """The PR endpoint omits reactions; the issue-form of a PR includes them.
    Returns the full reactions dict (per-emoji counts + total_count), or None."""
    if not pr_api_url or "/pulls/" not in pr_api_url:
        return None
    issue_url = pr_api_url.replace("/pulls/", "/issues/", 1)
    r = requests.get(issue_url, headers=_auth_headers(token), timeout=15)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json().get("reactions")


def _enrich(conn: sqlite3.Connection, token: str) -> None:
    """Fetch full details for up to ENRICHMENT_PER_POLL notifications that need it.

    For PRs, also fetches reactions via the issue-form (PR API omits them).
    Reaction fetch fires only when main details fetch fires, so cadence matches
    notification activity rather than poll frequency.
    """
    rows = conn.execute(
        """
        SELECT id, api_url, type FROM notifications
        WHERE type IN ('PullRequest', 'Issue')
          AND (
            details_json IS NULL
            OR details_fetched_at IS NULL
            OR datetime(updated_at) > datetime(details_fetched_at, 'unixepoch')
          )
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (ENRICHMENT_PER_POLL,),
    ).fetchall()

    for row in rows:
        try:
            details = fetch_details(token, row["api_url"])
        except Exception:
            log.exception("enrichment failed for %s", row["id"])
            continue
        now = int(time.time())
        if details is None:
            # 404 / unsupported — record the attempt so we don't retry forever.
            conn.execute(
                "UPDATE notifications SET details_fetched_at = ? WHERE id = ?",
                (now, row["id"]),
            )
            continue
        # COALESCE captures baseline_comments on first enrichment so the
        # '+N new comments' indicator stays alive through Read actions and
        # only shifts when actual notification activity changes the count.
        conn.execute(
            "UPDATE notifications SET details_json = ?, details_fetched_at = ?, "
            "baseline_comments = COALESCE(baseline_comments, ?) "
            "WHERE id = ?",
            (json.dumps(details), now, details.get("comments") or 0, row["id"]),
        )

        # Lazy-populate people directory from the author of the item.
        author = (details.get("user") or {})
        if author.get("login"):
            conn.execute(
                "INSERT INTO people (login, avatar_url, last_seen_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(login) DO UPDATE SET "
                "  avatar_url = excluded.avatar_url, "
                "  last_seen_at = excluded.last_seen_at",
                (author["login"], author.get("avatar_url"), now),
            )
        if row["type"] == "PullRequest":
            try:
                reactions = fetch_pr_reactions(token, row["api_url"])
                if reactions is not None:
                    conn.execute(
                        "UPDATE notifications "
                        "SET pr_reactions_json = ?, pr_reactions_fetched_at = ? "
                        "WHERE id = ?",
                        (json.dumps(reactions), now, row["id"]),
                    )
            except Exception:
                log.exception("pr_reactions fetch failed for %s", row["id"])
            try:
                review_state = fetch_pr_review_state(token, row["api_url"])
                # COALESCE captures baseline on the first time we know the
                # review state — the 'pill-new' dot only fires once it actually
                # changes after that point, surviving Read actions in between.
                conn.execute(
                    "UPDATE notifications SET pr_review_state = ?, "
                    "baseline_review_state = COALESCE(baseline_review_state, ?) "
                    "WHERE id = ?",
                    (review_state, review_state, row["id"]),
                )
            except Exception:
                log.exception("review state fetch failed for %s", row["id"])

        # Unique commenters (cheap when comments_count is 0).
        try:
            n_commenters = fetch_unique_commenters(
                token, row["api_url"], details.get("comments") or 0
            )
            if n_commenters is not None:
                conn.execute(
                    "UPDATE notifications SET unique_commenters = ? WHERE id = ?",
                    (n_commenters, row["id"]),
                )
        except Exception:
            log.exception("commenters fetch failed for %s", row["id"])


def set_subscribed(token: str, thread_id: str) -> None:
    """Re-subscribe to a thread (reverse of set_ignored)."""
    r = requests.put(
        f"{_thread_url(thread_id)}/subscription",
        headers=_auth_headers(token),
        json={"subscribed": True, "ignored": False},
        timeout=10,
    )
    if r.status_code == 200:
        return
    r.raise_for_status()


def _upsert(conn: sqlite3.Connection, item: dict[str, Any], now: int) -> None:
    subject = item.get("subject") or {}
    repo = item.get("repository") or {}
    reason = item.get("reason") or ""
    conn.execute(
        """
        INSERT INTO notifications (
            id, repo, type, title, reason, api_url, html_url,
            updated_at, last_read_at, unread, raw_json, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            repo=excluded.repo,
            type=excluded.type,
            title=excluded.title,
            reason=excluded.reason,
            api_url=excluded.api_url,
            html_url=excluded.html_url,
            updated_at=excluded.updated_at,
            last_read_at=excluded.last_read_at,
            unread=excluded.unread,
            raw_json=excluded.raw_json,
            fetched_at=excluded.fetched_at
        """,
        (
            item["id"],
            repo.get("full_name") or "",
            subject.get("type") or "",
            subject.get("title") or "",
            reason,
            subject.get("url"),
            derive_html_url(item),
            item.get("updated_at") or "",
            item.get("last_read_at"),
            1 if item.get("unread") else 0,
            json.dumps(item),
            now,
        ),
    )
    if reason:
        _accumulate_seen_reason(conn, item["id"], reason)


def _accumulate_seen_reason(
    conn: sqlite3.Connection, thread_id: str, reason: str
) -> None:
    """Union the current reason into the thread's seen_reasons (cleared on action)."""
    row = conn.execute(
        "SELECT seen_reasons FROM notifications WHERE id = ?", (thread_id,)
    ).fetchone()
    seen: set[str] = set()
    if row and row["seen_reasons"]:
        try:
            seen = set(json.loads(row["seen_reasons"]))
        except (ValueError, TypeError):
            pass
    if reason in seen:
        return
    seen.add(reason)
    conn.execute(
        "UPDATE notifications SET seen_reasons = ? WHERE id = ?",
        (json.dumps(sorted(seen)), thread_id),
    )


def _get_paginated(
    token: str,
    params: dict,
    last_modified: str | None,
    max_pages: int = MAX_PAGES_PER_FETCH,
) -> tuple[list[dict[str, Any]], str | None, int]:
    """GET /notifications with optional If-Modified-Since + page cap.
    Returns (items, new_last_modified, status). status=304 means no changes."""
    headers = _auth_headers(token)
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    r = requests.get(API_NOTIFICATIONS, headers=headers, params=params, timeout=30)
    if r.status_code == 304:
        return [], last_modified, 304
    r.raise_for_status()
    new_last_modified = r.headers.get("Last-Modified", last_modified)
    items: list[dict[str, Any]] = list(r.json())
    page_headers = _auth_headers(token)  # no If-Modified-Since on subsequent pages
    pages = 1
    while "next" in r.links and pages < max_pages:
        r = requests.get(r.links["next"]["url"], headers=page_headers, timeout=30)
        r.raise_for_status()
        items.extend(r.json())
        pages += 1
    return items, new_last_modified, 200


def _fetch_unread(conn: sqlite3.Connection, token: str) -> int:
    """Fetch currently-unread notifications + reconcile read-state of items
    missing from the response (they were marked read elsewhere). This is the
    only path that catches read-state changes on threads with no new activity
    (e.g. user clicked through on github.com/mobile without commenting)."""
    last_modified = (
        db.get_meta(conn, "last_modified_unread")
        or db.get_meta(conn, "last_modified")  # fallback to legacy single-key
    )
    items, new_last_modified, status = _get_paginated(
        token, {"per_page": PER_PAGE}, last_modified
    )
    if status == 304:
        return 0

    now = int(time.time())
    seen_ids: set[str] = set()
    for item in items:
        if not item.get("id"):
            continue
        seen_ids.add(item["id"])
        _upsert(conn, item, now)

    # Items previously unread but missing from the response were read elsewhere.
    # Skip rows where the user has explicitly kept-unread locally.
    if seen_ids:
        placeholders = ",".join(["?"] * len(seen_ids))
        conn.execute(
            f"UPDATE notifications SET unread=0 "
            f"WHERE unread=1 "
            f"AND COALESCE(action, '') != 'kept_unread' "
            f"AND id NOT IN ({placeholders})",
            tuple(seen_ids),
        )
    else:
        conn.execute(
            "UPDATE notifications SET unread=0 "
            "WHERE unread=1 AND COALESCE(action, '') != 'kept_unread'"
        )

    if new_last_modified:
        db.set_meta(conn, "last_modified_unread", new_last_modified)
        if db.get_meta(conn, "last_modified"):
            db.set_meta(conn, "last_modified", None)  # cleanup legacy key
    return len(items)


def _fetch_since(conn: sqlite3.Connection, token: str) -> int:
    """Fetch every notification updated since last successful since-fetch
    (?all=true&since=<bookmark>). Catches arrivals that were created and read
    on another client before we had a chance to see them as unread."""
    since = db.get_meta(conn, "last_full_fetch_at")
    last_modified = db.get_meta(conn, "last_modified_all")
    fetched_at_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    params: dict = {"per_page": PER_PAGE, "all": "true"}
    if since:
        params["since"] = since

    items, new_last_modified, status = _get_paginated(token, params, last_modified)

    if status == 200:
        now = int(time.time())
        for item in items:
            if not item.get("id"):
                continue
            _upsert(conn, item, now)
        if new_last_modified:
            db.set_meta(conn, "last_modified_all", new_last_modified)

    # Advance the bookmark on success or 304. On exception we never reach here
    # so the bookmark stays put — next attempt re-fetches from the same point.
    db.set_meta(conn, "last_full_fetch_at", fetched_at_iso)
    return len(items) if status == 200 else 0


def poll_once(conn: sqlite3.Connection, token: str) -> int:
    """Run both fetches (unread + since-last) and enrich PR/Issue details.
    Each fetch is bounded by MAX_PAGES_PER_FETCH * PER_PAGE items (~100);
    larger backfills go through Backfetch. Returns total items touched."""
    n_unread = _fetch_unread(conn, token)
    n_since = _fetch_since(conn, token)
    _enrich(conn, token)
    return n_unread + n_since
