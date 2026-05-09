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


def backfetch(conn: sqlite3.Connection, token: str, n: int = 50) -> int:
    """Pull the last N notifications (including read) and force re-enrichment
    of those rows. Paginates as needed up to N items. Used as an explicit
    backfill for initial population or catching up after long offline periods.

    Note: enrichment runs once with the existing ENRICHMENT_PER_POLL bound, so
    on large backfills the first batch enriches now and the rest catch up over
    subsequent polls (their details_fetched_at is NULL, so they get picked).
    """
    per_page = min(100, max(1, n))
    headers = _auth_headers(token)
    items: list[dict[str, Any]] = []
    next_url: str | None = None

    while len(items) < n:
        if next_url:
            r = requests.get(next_url, headers=headers, timeout=30)
        else:
            r = requests.get(
                API_NOTIFICATIONS,
                headers=headers,
                params={"per_page": per_page, "all": "true"},
                timeout=30,
            )
        r.raise_for_status()
        items.extend(r.json())
        next_url = r.links.get("next", {}).get("url")
        if not next_url:
            break
    items = items[:n]

    now = int(time.time())
    ids: list[str] = []
    for item in items:
        if not item.get("id"):
            continue
        ids.append(item["id"])
        _upsert(conn, item, now)

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


def fetch_pr_reviews(token: str, pr_api_url: str | None) -> list[dict] | None:
    """Fetch all reviews on a PR. Returns the raw review list, or None if the
    URL isn't a PR or the resource is gone. Caller derives state + reviewer
    count from the same response so we only hit /reviews once per enrichment."""
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
    return r.json()


def _count_unique_reviewers(reviews: list[dict]) -> int:
    """Distinct review-author logins, excluding PENDING drafts (those are
    in-progress reviews not yet visible to anyone else)."""
    logins: set[str] = set()
    for r in reviews:
        if r.get("state") == "PENDING":
            continue
        login = (r.get("user") or {}).get("login")
        if login:
            logins.add(login)
    return len(logins)


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


_GRAPHQL_URL = "https://api.github.com/graphql"

# GraphQL ReactionContent enum -> the REST reaction-key the rest of the app
# expects in details_json["reactions"].
_GRAPHQL_REACTION_KEYS = {
    "THUMBS_UP": "+1",
    "THUMBS_DOWN": "-1",
    "LAUGH": "laugh",
    "HOORAY": "hooray",
    "CONFUSED": "confused",
    "HEART": "heart",
    "ROCKET": "rocket",
    "EYES": "eyes",
}

_DISCUSSION_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    discussion(number: $number) {
      number
      title
      url
      createdAt
      updatedAt
      author { login avatarUrl }
      comments(first: 100) {
        totalCount
        nodes {
          author { login }
          replies(first: 100) { nodes { author { login } } }
        }
      }
      reactionGroups { content reactors { totalCount } }
    }
  }
}
"""


def _parse_discussion_url(api_url: str | None) -> tuple[str, str, int] | None:
    """Extract (owner, name, number) from .../repos/{owner}/{name}/discussions/{n}."""
    if not api_url or "/discussions/" not in api_url:
        return None
    prefix = "https://api.github.com/repos/"
    if not api_url.startswith(prefix):
        return None
    parts = api_url[len(prefix):].split("/")
    # owner / name / "discussions" / number
    if len(parts) < 4 or parts[2] != "discussions":
        return None
    try:
        return parts[0], parts[1], int(parts[3])
    except ValueError:
        return None


def fetch_discussion(token: str, api_url: str | None) -> dict | None:
    """GraphQL fetch for a Discussion (REST has no equivalent endpoint).

    Returns a payload shaped like a REST Issue (reactions dict + comments count
    + user) so it can flow through the same details_json path as Issues, with
    one bonus key '_unique_commenters' (folded in here because we already paged
    the comments to compute it).
    """
    parsed = _parse_discussion_url(api_url)
    if parsed is None:
        return None
    owner, name, number = parsed
    headers = {**_auth_headers(token), "Content-Type": "application/json"}
    r = requests.post(
        _GRAPHQL_URL,
        headers=headers,
        json={
            "query": _DISCUSSION_QUERY,
            "variables": {"owner": owner, "name": name, "number": number},
        },
        timeout=20,
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    payload = r.json()
    if payload.get("errors"):
        log.warning(
            "GraphQL errors fetching discussion %s/%s#%s: %s",
            owner, name, number, payload["errors"],
        )
        return None
    disc = ((payload.get("data") or {}).get("repository") or {}).get("discussion")
    if not disc:
        return None

    reactions: dict[str, int] = {v: 0 for v in _GRAPHQL_REACTION_KEYS.values()}
    total = 0
    for g in disc.get("reactionGroups") or []:
        n = ((g.get("reactors") or {}).get("totalCount")) or 0
        key = _GRAPHQL_REACTION_KEYS.get(g.get("content"))
        if key is not None:
            reactions[key] = n
        total += n
    reactions["total_count"] = total

    comments = disc.get("comments") or {}
    comment_total = comments.get("totalCount") or 0
    logins: set[str] = set()
    for c in comments.get("nodes") or []:
        login = (c.get("author") or {}).get("login")
        if login:
            logins.add(login)
        for rep in ((c.get("replies") or {}).get("nodes")) or []:
            rl = (rep.get("author") or {}).get("login")
            if rl:
                logins.add(rl)

    author = disc.get("author") or {}
    return {
        "html_url": disc.get("url"),
        "created_at": disc.get("createdAt"),
        "user": {
            "login": author.get("login"),
            "avatar_url": author.get("avatarUrl"),
        },
        "comments": comment_total,
        "reactions": reactions,
        "_unique_commenters": len(logins),
    }


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
        WHERE type IN ('PullRequest', 'Issue', 'Discussion')
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
            if row["type"] == "Discussion":
                details = fetch_discussion(token, row["api_url"])
            else:
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

        if row["type"] == "Discussion":
            # GraphQL fetch already includes unique_commenters; skip the REST
            # commenters call (the discussion comments endpoint doesn't exist
            # in REST). Reactions are embedded in details_json like Issues.
            n_commenters = details.get("_unique_commenters")
            if n_commenters is not None:
                conn.execute(
                    "UPDATE notifications SET unique_commenters = ? WHERE id = ?",
                    (n_commenters, row["id"]),
                )
            continue

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
                reviews = fetch_pr_reviews(token, row["api_url"])
                if reviews is not None:
                    review_state = _compute_review_state(reviews)
                    n_reviewers = _count_unique_reviewers(reviews)
                    # COALESCE captures baseline on the first time we know the
                    # review state — the 'pill-new' dot only fires once it
                    # actually changes after that point, surviving Read actions
                    # in between.
                    conn.execute(
                        "UPDATE notifications SET pr_review_state = ?, "
                        "baseline_review_state = COALESCE(baseline_review_state, ?), "
                        "unique_reviewers = ? "
                        "WHERE id = ?",
                        (review_state, review_state, n_reviewers, row["id"]),
                    )
            except Exception:
                log.exception("review fetch failed for %s", row["id"])

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
