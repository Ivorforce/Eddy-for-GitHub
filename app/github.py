"""GitHub /notifications fetcher + upsert into SQLite."""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from typing import Any

import requests

from . import db, oauth, settings

API_NOTIFICATIONS = "https://api.github.com/notifications"
API_SEARCH_ISSUES = "https://api.github.com/search/issues"
# Search-backfill scopes: which `is:open` issues/PRs to pull in as synthetic
# notification rows. `involves:@me` is a superset of `author:@me` (also
# assignee / commenter / mentioned).
_SEARCH_SCOPES = {"authored": "author:@me", "involved": "involves:@me"}
PER_PAGE = 50
MAX_PAGES_PER_FETCH = 2  # ~100 items per fetch — bound auto-poll cost
MAX_PAGES_FORCED = 20    # ~1000 items on a manual refresh / app launch
ENRICHMENT_PER_POLL = 20

log = logging.getLogger(__name__)

# Module-level session so HTTP keep-alive + connection pooling apply across
# every call to api.github.com — saves a TCP + TLS handshake per request,
# which adds up when _enrich runs ~20 calls back-to-back.
_session = requests.Session()

# Authenticated user's GitHub login, set at startup from auth.fetch_identity.
# _enrich consults this to detect self-authored comments / reviews / body
# edits — a self-comment proves the user looked at the thread, so the
# baselines get reset like any other engagement event. None falls back to
# "no self-authorship detection" (degrades gracefully on identity failures).
_USER_LOGIN: str | None = None


def set_user_login(login: str | None) -> None:
    global _USER_LOGIN
    _USER_LOGIN = login


# Per-thread enrichment signal. A row is "dirty" (enrichment owed) iff
# `details_fetched_at IS NULL OR datetime(updated_at) > datetime(details_fetched_at,
# 'unixepoch')` — same predicate the _enrich eligibility query uses. The Event
# is the in-memory edge of that DB state: cleared when _upsert lands new
# activity that makes the row dirty, set when _enrich finishes (or 404s) and
# writes details_fetched_at. AI callers wait on it before sending the prompt,
# so they don't judge against stale thread_events.
_ENRICH_SIGNAL_LOCK = threading.Lock()
_ENRICH_SIGNALS: dict[str, threading.Event] = {}


def _is_dirty(conn: sqlite3.Connection, thread_id: str) -> bool:
    """The dirty predicate. Returns True iff the row needs (re-)enrichment."""
    row = conn.execute(
        "SELECT updated_at, details_fetched_at FROM notifications WHERE id = ?",
        (thread_id,),
    ).fetchone()
    if row is None:
        return False
    if row["details_fetched_at"] is None:
        # No detail fetch yet (e.g., a Release row, or pre-enrichment). Only
        # treat as dirty if the row has a populated updated_at — otherwise
        # there's nothing for _enrich to compare against.
        return bool(row["updated_at"])
    # SQLite datetime() handles the unix-epoch vs ISO comparison; mirror
    # _enrich's eligibility query exactly so the predicate doesn't drift.
    cmp = conn.execute(
        "SELECT datetime(?) > datetime(?, 'unixepoch') AS dirty",
        (row["updated_at"], row["details_fetched_at"]),
    ).fetchone()
    return bool(cmp["dirty"])


def _enrich_signal(conn: sqlite3.Connection, thread_id: str) -> threading.Event:
    """Lookup-or-create the Event for `thread_id`. New Events are initialized
    against the current DB predicate — a clean row starts set so waiters don't
    block forever on a row we've never touched; a dirty row starts cleared."""
    with _ENRICH_SIGNAL_LOCK:
        ev = _ENRICH_SIGNALS.get(thread_id)
        if ev is not None:
            return ev
        ev = threading.Event()
        if not _is_dirty(conn, thread_id):
            ev.set()
        _ENRICH_SIGNALS[thread_id] = ev
        return ev


def wait_until_enriched(
    conn: sqlite3.Connection, thread_id: str, *, timeout: float = 60.0
) -> bool:
    """Block until the row's enrichment is up to date, or `timeout` elapses.

    Returns True if the row is clean (now or after waiting), False if it's
    still dirty when we give up. Callers raise their own error on False —
    judging a stale timeline is worse than failing the call.

    The post-wait DB re-check is load-bearing: poll N's _enrich can set the
    signal carrying a details_fetched_at that's already stale because poll N+1
    bumped updated_at in between. Trusting the Event alone would let the
    waiter proceed on data the next enrichment will overwrite."""
    if not _is_dirty(conn, thread_id):
        return True
    ev = _enrich_signal(conn, thread_id)
    ev.wait(timeout=timeout)
    return not _is_dirty(conn, thread_id)


def _mark_enriched(thread_id: str) -> None:
    """Signal waiters that this row is freshly enriched. Safe to call even if
    no Event exists yet — we create one set."""
    with _ENRICH_SIGNAL_LOCK:
        ev = _ENRICH_SIGNALS.get(thread_id)
        if ev is None:
            ev = threading.Event()
            _ENRICH_SIGNALS[thread_id] = ev
        ev.set()


def _mark_dirty(thread_id: str) -> None:
    """Signal waiters that this row has fresh activity pending enrichment.
    Lazily creates a cleared Event if none existed yet."""
    with _ENRICH_SIGNAL_LOCK:
        ev = _ENRICH_SIGNALS.get(thread_id)
        if ev is None:
            ev = threading.Event()  # starts cleared
            _ENRICH_SIGNALS[thread_id] = ev
            return
        ev.clear()


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


_API_REPOS_PREFIX = "https://api.github.com/repos/"


def derive_link_url(item: dict[str, Any]) -> str | None:
    """Convert subject.latest_comment_url to a per-event github.com URL.

    The Notifications API gives us only the latest event URL — not a list —
    so this is the best we can pin while a thread is unread. Falls back to
    None on unrecognized shapes (Releases, CheckSuites, Discussions); caller
    uses html_url instead.

    Patterns:
      issues/comments/{cid}        → /issues/{n}#issuecomment-{cid}      (Issue)
      issues/comments/{cid}        → /pull/{n}#issuecomment-{cid}        (PR thread comment)
      pulls/comments/{cid}         → /pull/{n}#discussion_r{cid}         (review-line)
      pulls/{n}/reviews/{rid}      → /pull/{n}#pullrequestreview-{rid}   (review)
    """
    subject = item.get("subject") or {}
    api_comment = subject.get("latest_comment_url")
    api_subject = subject.get("url")
    if not api_comment or not api_subject or api_comment == api_subject:
        return None
    if not (api_comment.startswith(_API_REPOS_PREFIX)
            and api_subject.startswith(_API_REPOS_PREFIX)):
        return None

    subj_parts = api_subject[len(_API_REPOS_PREFIX):].split("/")
    com_parts = api_comment[len(_API_REPOS_PREFIX):].split("/")
    if len(subj_parts) < 4 or len(com_parts) < 5:
        return None
    owner, repo, kind, number = subj_parts[0], subj_parts[1], subj_parts[2], subj_parts[3]
    base = f"https://github.com/{owner}/{repo}"

    # /repos/{o}/{r}/issues/comments/{cid} — generic issue/PR thread comment.
    if com_parts[2] == "issues" and com_parts[3] == "comments":
        cid = com_parts[4]
        if kind == "pulls":
            return f"{base}/pull/{number}#issuecomment-{cid}"
        if kind == "issues":
            return f"{base}/issues/{number}#issuecomment-{cid}"
        return None
    # /repos/{o}/{r}/pulls/comments/{cid} — review-line comment.
    if com_parts[2] == "pulls" and com_parts[3] == "comments":
        cid = com_parts[4]
        return f"{base}/pull/{number}#discussion_r{cid}"
    # /repos/{o}/{r}/pulls/{n}/reviews/{rid}
    if (com_parts[2] == "pulls" and len(com_parts) >= 6
            and com_parts[4] == "reviews"):
        rid = com_parts[5]
        return f"{base}/pull/{number}#pullrequestreview-{rid}"
    return None


def compute_action_needed(
    *,
    details: dict | None,
    repo_owner: str | None,
    current_reason: str | None,
    seen: set[str],
    user_login: str | None,
    user_teams: set[tuple[str, str]],
) -> str | None:
    """Bucket a thread by the response it asks of the user — one of
    `assigned` / `review_you` / `review_team` / None.

    Prefers cached details (`details_json`) for accuracy; falls back to the
    notification reason when details aren't available yet. Pure function —
    takes user identity as explicit params so it can be called from web
    routes (Flask-aware) and from the AI / poll loop (no Flask context).
    """
    if details:
        if user_login and any(
            (a or {}).get("login") == user_login
            for a in details.get("assignees") or []
        ):
            return "assigned"
        if user_login and any(
            (r or {}).get("login") == user_login
            for r in details.get("requested_reviewers") or []
        ):
            return "review_you"
        if user_teams and repo_owner:
            for t in details.get("requested_teams") or []:
                slug = (t or {}).get("slug")
                if slug and (repo_owner, slug) in user_teams:
                    return "review_team"
        return None

    # No cached details — best-effort hint from reason. We don't know
    # you-vs-team for review_requested without details, so default to
    # review_team (the more common case for org maintainers); enrichment
    # will correct on next poll.
    if "assign" in seen or current_reason == "assign":
        return "assigned"
    if "review_requested" in seen or current_reason == "review_requested":
        return "review_team"
    return None


def _auth_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _thread_url(thread_id: str) -> str:
    return f"https://api.github.com/notifications/threads/{thread_id}"


# Synthetic search-backfilled rows carry an id like "q_<node_id>" — there's no
# GitHub notification thread behind them, so the thread-mutating calls below
# short-circuit. Acting on such a row updates local state only (via
# _apply_action); in particular Mute won't actually unsubscribe on GitHub. If
# real activity later delivers a genuine notification for that thread, _upsert's
# de-dup drops the synthetic row and the real one comes in unmuted — acceptable.
# The id lands verbatim in DOM ids / CSS selectors / CSS custom-idents (e.g.
# `anchor-name: --pop-timeline-<id>` and HTMX `from:#pop-timeline-<id>` triggers),
# so it has to be a valid CSS identifier tail. Real thread ids are all-digits,
# so "q_" is unambiguous. Modern node_ids are `[A-Za-z0-9_-]` (fine), but the
# legacy base64-with-padding form (`MDU...=`) carries a trailing `=` that is
# *not* valid in CSS identifiers — strip it. Padding is length-derived, so
# dropping it is bijective; nothing else round-trips the id back to a node_id.
def _synth_id(node_id: str) -> str:
    return "q_" + node_id.rstrip("=")


def _is_synthetic(thread_id: str) -> bool:
    return thread_id.startswith("q_")


def mark_read(token: str, thread_id: str) -> None:
    """Mark a notification thread as read. Stays in the inbox."""
    if _is_synthetic(thread_id):
        return
    r = _session.patch(_thread_url(thread_id), headers=_auth_headers(token), timeout=10)
    if r.status_code in (200, 205, 304):
        return
    r.raise_for_status()


def mark_done(token: str, thread_id: str) -> None:
    """Mark as done — clears the notification from the inbox."""
    if _is_synthetic(thread_id):
        return
    r = _session.delete(_thread_url(thread_id), headers=_auth_headers(token), timeout=10)
    if r.status_code in (204, 404):
        return  # 404 if already gone — idempotent
    r.raise_for_status()


def set_ignored(token: str, thread_id: str) -> None:
    """Set thread subscription to ignored — stops future notifications on this thread."""
    if _is_synthetic(thread_id):
        return
    r = _session.put(
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
            r = _session.get(next_url, headers=headers, timeout=30)
        else:
            r = _session.get(
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
            f"UPDATE notifications SET details_fetched_at = NULL "
            f"WHERE id IN ({placeholders})",
            tuple(ids),
        )
    _enrich(conn, token)
    return len(items)


def backfetch_issues(
    conn: sqlite3.Connection, token: str, scope: str, n: int = 50
) -> int:
    """Pull the latest N open issues/PRs matching `scope` (authored / involved)
    from the search API and insert them as synthetic notification rows
    (id "q_<node_id>", unread=0), then force enrichment like `backfetch`.

    These aren't real notifications — GitHub has no notification-thread handle
    for an arbitrary issue/PR — so the row's id is synthetic and the
    thread-mutating actions short-circuit (see _is_synthetic). Skips a result
    if a real notification row already owns that html_url; the reverse case
    (a real notification arriving later) is handled by _upsert's de-dup.
    """
    qualifier = _SEARCH_SCOPES[scope]
    n = max(1, min(n, 1000))  # search API returns at most 1000 results
    q = f"is:open archived:false {qualifier}"
    per_page = min(100, n)
    headers = _auth_headers(token)
    # `author` for the authored scope; for the involved scope use `manual` —
    # the search query is a deliberate inclusion, not a passive watch, and
    # `manual` ∈ INVOLVED_REASONS so these rows show under "Involved only"
    # (search doesn't tell us per-result whether you're assignee / commenter /
    # mentioned, so the exact directed reason isn't recoverable).
    reason = "author" if scope == "authored" else "manual"

    items: list[dict[str, Any]] = []
    next_url: str | None = None
    while len(items) < n:
        if next_url:
            r = _session.get(next_url, headers=headers, timeout=30)
        else:
            r = _session.get(
                API_SEARCH_ISSUES,
                headers=headers,
                params={"q": q, "sort": "updated", "order": "desc",
                        "per_page": per_page},
                timeout=30,
            )
        r.raise_for_status()
        items.extend(r.json().get("items", []))
        next_url = r.links.get("next", {}).get("url")
        if not next_url:
            break
    items = items[:n]

    now = int(time.time())
    ids: list[str] = []
    for it in items:
        html = it.get("html_url")
        node_id = it.get("node_id")
        repo_url = it.get("repository_url") or ""
        if not html or not node_id or not repo_url.startswith(_API_REPOS_PREFIX):
            continue
        # A real notification row, if one exists, owns this thread — leave it.
        if conn.execute(
            "SELECT 1 FROM notifications WHERE html_url = ? AND id NOT LIKE 'q\\_%' ESCAPE '\\'",
            (html,),
        ).fetchone():
            continue
        is_pr = "pull_request" in it
        # search `url` is always .../issues/{n}; fetch_pr wants .../pulls/{n}.
        api_url = (it.get("pull_request") or {}).get("url") if is_pr else it.get("url")
        if not api_url:
            continue
        repo_full = repo_url[len(_API_REPOS_PREFIX):]
        synth = {
            "id": _synth_id(node_id),
            "unread": False,
            "reason": reason,
            "updated_at": it.get("updated_at") or "",
            "last_read_at": None,
            "subject": {
                "type": "PullRequest" if is_pr else "Issue",
                "title": it.get("title") or "",
                "url": api_url,
                "latest_comment_url": None,
            },
            "repository": {
                "full_name": repo_full,
                "html_url": f"https://github.com/{repo_full}",
            },
        }
        ids.append(synth["id"])
        _upsert(conn, synth, now)

    if ids:
        placeholders = ",".join(["?"] * len(ids))
        conn.execute(
            f"UPDATE notifications SET details_fetched_at = NULL "
            f"WHERE id IN ({placeholders})",
            tuple(ids),
        )
    _enrich(conn, token)
    return len(items)


# github.com/{owner}/{repo}/{issues|pull|discussions}/{n} — tolerates trailing
# path (/files, /commits), query, and fragment (#issuecomment-…).
_ITEM_HTML_URL_RE = re.compile(
    r"^https?://github\.com/([^/\s]+)/([^/\s]+)/(issues|pull|discussions)/(\d+)\b"
)


def track_link(conn: sqlite3.Connection, token: str, url: str) -> str:
    """Resolve a pasted github.com issue / PR / discussion URL into a synthetic
    notification row (id "q_<node_id>", reason "manual", unread=0) and enrich
    it — the single-item cousin of backfetch_issues. Returns "added", or
    "exists" when a real or synthetic row already covers that thread; raises
    ValueError on a malformed or unreachable URL.
    """
    m = _ITEM_HTML_URL_RE.match(url.strip())
    if not m:
        raise ValueError("Not a GitHub issue / PR / discussion link")
    owner, repo, kind, number = m.group(1), m.group(2), m.group(3), int(m.group(4))
    headers = _auth_headers(token)

    if kind == "discussions":
        # No REST endpoint for discussions — one GraphQL hop for id/title/url.
        r = _session.post(
            _GRAPHQL_URL,
            headers={**headers, "Content-Type": "application/json"},
            json={"query": _DISCUSSION_STUB_QUERY,
                  "variables": {"owner": owner, "name": repo, "number": number}},
            timeout=20,
        )
        r.raise_for_status()
        disc = (((r.json().get("data") or {}).get("repository") or {})
                .get("discussion"))
        if not disc or not disc.get("id"):
            raise ValueError(f"Discussion {owner}/{repo}#{number} not found")
        node_id, title = disc["id"], disc.get("title") or ""
        updated_at = disc.get("updatedAt") or ""
        html_url = disc.get("url") or f"https://github.com/{owner}/{repo}/discussions/{number}"
        api_url = f"https://api.github.com/repos/{owner}/{repo}/discussions/{number}"
        subject_type = "Discussion"
    else:
        # /issues/{n} resolves PRs too; `pull_request` in the payload says which.
        r = _session.get(
            f"https://api.github.com/repos/{owner}/{repo}/issues/{number}",
            headers=headers, timeout=30,
        )
        if r.status_code == 404:
            raise ValueError(f"{owner}/{repo}#{number} not found")
        r.raise_for_status()
        it = r.json()
        node_id, title = it.get("node_id"), it.get("title") or ""
        updated_at = it.get("updated_at") or ""
        is_pr = "pull_request" in it
        html_url = it.get("html_url") or f"https://github.com/{owner}/{repo}/{kind}/{number}"
        api_url = (it.get("pull_request") or {}).get("url") if is_pr else it.get("url")
        subject_type = "PullRequest" if is_pr else "Issue"
        if not node_id or not api_url:
            raise ValueError("Unexpected GitHub response")

    # Already covered by a real notification or an earlier synthetic row? Leave
    # it — adding the link means "show me this", not "resurrect this".
    if conn.execute(
        "SELECT 1 FROM notifications WHERE html_url = ?", (html_url,)
    ).fetchone():
        return "exists"

    now = int(time.time())
    synth = {
        "id": _synth_id(node_id),
        "unread": False,
        "reason": "manual",  # ∈ INVOLVED_REASONS — a deliberate inclusion
        "updated_at": updated_at,
        "last_read_at": None,
        "subject": {
            "type": subject_type, "title": title, "url": api_url,
            "latest_comment_url": None,
        },
        "repository": {
            "full_name": f"{owner}/{repo}",
            "html_url": f"https://github.com/{owner}/{repo}",
        },
    }
    _upsert(conn, synth, now)
    conn.execute(
        "UPDATE notifications SET details_fetched_at = NULL WHERE id = ?",
        (synth["id"],),
    )
    _enrich(conn, token)
    return "added"


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


def _condense_reaction_groups(groups) -> dict | None:
    """reactionGroups list → {emoji_key: count, …, "total_count": N} with
    zero-count emoji omitted, or None when there are no reactions at all.

    The lean per-comment / per-review / per-linked-issue shape — distinct from
    the top-level item `reactions` dict, which keeps every key (zeros included)
    because the positive-max popularity aggregator iterates a fixed key set.
    """
    out: dict[str, int] = {}
    total = 0
    for g in groups or []:
        n = ((g.get("reactors") or {}).get("totalCount")) or 0
        if n <= 0:
            continue
        key = _GRAPHQL_REACTION_KEYS.get(g.get("content"))
        if key is not None:
            out[key] = n
        total += n
    if total <= 0:
        return None
    out["total_count"] = total
    return out


def _comment_node_extras(n: dict) -> dict:
    """Per-comment fields beyond the core set, as a dict to splat into the
    comment_history entry: `reactions` (when any), and `minimized_reason`
    (`off-topic` / `outdated` / `resolved` / `duplicate` / `spam` / `abuse` /
    `hidden` fallback) when a maintainer has collapsed the comment. Empty when
    neither applies."""
    out: dict = {}
    rx = _condense_reaction_groups(n.get("reactionGroups"))
    if rx:
        out["reactions"] = rx
    if n.get("isMinimized"):
        out["minimized_reason"] = (n.get("minimizedReason") or "hidden").lower()
    return out


_DISCUSSION_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    discussion(number: $number) {
      number
      title
      body
      lastEditedAt
      editor { login }
      url
      createdAt
      updatedAt
      closed
      isAnswered
      upvoteCount
      category { name }
      author { login avatarUrl }
      comments(last: 100) {
        totalCount
        nodes {
          databaseId
          author { login }
          authorAssociation
          body
          createdAt
          lastEditedAt
          isMinimized
          minimizedReason
          reactionGroups { content reactors { totalCount } }
          replies(last: 3) { totalCount nodes { author { login } body createdAt } }
        }
      }
      reactionGroups { content reactors { totalCount } }
    }
  }
}
"""

# Just enough to mint a synthetic row for a pasted discussion link (no REST
# endpoint exists); _enrich re-fetches the full thread via _DISCUSSION_QUERY.
_DISCUSSION_STUB_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    discussion(number: $number) { id title url updatedAt }
  }
}
"""


def _parse_repo_url(
    api_url: str | None, segment: str
) -> tuple[str, str, int] | None:
    """Extract (owner, name, number) from .../repos/{owner}/{name}/{segment}/{n}.
    `segment` is 'discussions' for Discussion URLs, 'pulls' for PR URLs."""
    if not api_url or f"/{segment}/" not in api_url:
        return None
    prefix = "https://api.github.com/repos/"
    if not api_url.startswith(prefix):
        return None
    parts = api_url[len(prefix):].split("/")
    if len(parts) < 4 or parts[2] != segment:
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
    parsed = _parse_repo_url(api_url, "discussions")
    if parsed is None:
        return None
    owner, name, number = parsed
    headers = {**_auth_headers(token), "Content-Type": "application/json"}
    r = _session.post(
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
    # Upvotes fold into the same reactions dict so the positive-max
    # aggregator picks them up via _POSITIVE_REACTIONS without a special
    # case. Discussion-only signal; other types omit the key.
    reactions["upvotes"] = disc.get("upvoteCount") or 0

    comments = disc.get("comments") or {}
    comment_total = comments.get("totalCount") or 0
    logins: set[str] = set()
    comment_history: list[dict] = []
    nodes = comments.get("nodes") or []
    # A discussion's substance often lives in the reply sub-threads, not the
    # top-level comments — so each comment carries its reply tail (sampled
    # like review threads: last few, verbatim & truncated) plus a total
    # `reply_count` so a gap is visible. Bodies only on the most-recent
    # _DISCUSSION_REPLY_BODY_CAP comments — beyond that a 100-comment
    # discussion is an obvious `look` and count-only is enough. Commenter
    # counting now sees only the sampled replies' authors, a minor undercount
    # on heavily-replied threads (top-level authors dominate the figure).
    bodied_from = max(0, len(nodes) - _DISCUSSION_REPLY_BODY_CAP)
    for i, c in enumerate(nodes):
        login = (c.get("author") or {}).get("login")
        if login:
            logins.add(login)
        replies = c.get("replies") or {}
        reply_nodes = replies.get("nodes") or []
        for rep in reply_nodes:
            rl = (rep.get("author") or {}).get("login")
            if rl:
                logins.add(rl)
        entry = {
            "database_id": c.get("databaseId"),
            "user": {"login": login},
            "author_association": c.get("authorAssociation"),
            "created_at": c.get("createdAt"),
            "edited_at": c.get("lastEditedAt"),
            "body": c.get("body") or "",
            **_comment_node_extras(c),
        }
        reply_count = replies.get("totalCount") or len(reply_nodes)
        if reply_count:
            entry["reply_count"] = reply_count
            if i >= bodied_from and reply_nodes:
                entry["replies_sample"] = [_sampled_reply(r) for r in reply_nodes]
        comment_history.append(entry)

    # State flows through the same field as Issues so _type_state can
    # branch on it. 'answered' wins over 'closed' — an answered then
    # closed Q&A still reads as a successful outcome.
    if disc.get("isAnswered"):
        state = "answered"
    elif disc.get("closed"):
        state = "closed"
    else:
        state = "open"

    author = disc.get("author") or {}
    return {
        "html_url": disc.get("url"),
        "created_at": disc.get("createdAt"),
        "body": disc.get("body"),
        "body_edited_at": disc.get("lastEditedAt"),
        "body_editor": (disc.get("editor") or {}).get("login"),
        "state": state,
        "category": (disc.get("category") or {}).get("name"),
        "user": {
            "login": author.get("login"),
            "avatar_url": author.get("avatarUrl"),
        },
        "comments": comment_total,
        "_comment_history": comment_history,
        "reactions": reactions,
        "_unique_commenters": len(logins),
    }


# Single GraphQL query that replaces the four sequential REST calls (PR
# details, issue-form reactions, /reviews, /issues/N/comments). Connection
# limits are sized to cover the long tail without inflating points cost:
# 100 covers virtually every PR's commenters and reviews; 20 covers labels
# and review requests on even chunky PRs; 10 for assignees (rarely > 2).
_PR_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      number
      title
      body
      lastEditedAt
      editor { login }
      url
      createdAt
      updatedAt
      state
      isDraft
      merged
      mergeStateStatus
      reviewDecision
      additions
      deletions
      changedFiles
      files(first: 100) {
        nodes {
          path
          additions
          deletions
        }
      }
      authorAssociation
      author { login avatarUrl }
      assignees(first: 10) { nodes { login } }
      reviewRequests(first: 20) {
        nodes {
          asCodeOwner
          requestedReviewer {
            __typename
            ... on User { login }
            ... on Team { slug }
          }
        }
      }
      labels(first: 20) { nodes { name color description } }
      commits(last: 1) {
        totalCount
        nodes {
          commit {
            abbreviatedOid
            messageHeadline
            committedDate
            author { name user { login } }
            statusCheckRollup {
              state
              contexts(first: 100) {
                totalCount
                nodes {
                  __typename
                  ... on CheckRun { name conclusion status }
                  ... on StatusContext { context state }
                }
              }
            }
          }
        }
      }
      reactionGroups { content reactors { totalCount } }
      closingIssuesReferences(first: 10) {
        nodes { number title state reactionGroups { content reactors { totalCount } } }
      }
      projectItems(first: 10, includeArchived: false) {
        nodes {
          project { title closed }
          fieldValues(first: 20) {
            nodes {
              __typename
              ... on ProjectV2ItemFieldSingleSelectValue { name field { ... on ProjectV2FieldCommon { name } } }
              ... on ProjectV2ItemFieldTextValue         { text field { ... on ProjectV2FieldCommon { name } } }
              ... on ProjectV2ItemFieldNumberValue       { number field { ... on ProjectV2FieldCommon { name } } }
              ... on ProjectV2ItemFieldDateValue         { date field { ... on ProjectV2FieldCommon { name } } }
              ... on ProjectV2ItemFieldIterationValue    { title field { ... on ProjectV2FieldCommon { name } } }
            }
          }
        }
      }
      reviewThreads(last: 100) {
        nodes {
          isResolved
          isOutdated
          path
          opener: comments(first: 1) { nodes { author { login } body } }
          latest: comments(last: 2) { totalCount nodes { createdAt author { login } body } }
        }
      }
      comments(last: 100) {
        totalCount
        nodes {
          databaseId
          author { login }
          authorAssociation
          body
          createdAt
          lastEditedAt
          isMinimized
          minimizedReason
          reactionGroups { content reactors { totalCount } }
        }
      }
      reviews(last: 100) {
        nodes {
          databaseId
          state
          author { login }
          authorAssociation
          body
          submittedAt
          lastEditedAt
          comments { totalCount }
          reactionGroups { content reactors { totalCount } }
        }
      }
      timelineItems(last: 50, itemTypes: [
        MERGED_EVENT, CLOSED_EVENT, REOPENED_EVENT,
        READY_FOR_REVIEW_EVENT, CONVERT_TO_DRAFT_EVENT
      ]) {
        nodes {
          __typename
          ... on MergedEvent          { id createdAt actor { login } }
          ... on ClosedEvent          { id createdAt actor { login } stateReason }
          ... on ReopenedEvent        { id createdAt actor { login } }
          ... on ReadyForReviewEvent  { id createdAt actor { login } }
          ... on ConvertToDraftEvent  { id createdAt actor { login } }
        }
      }
    }
  }
}
"""


# GraphQL __typename → our payload's `action` string. The kind covers all
# state transitions we surface; the action discriminates what actually
# happened. Centralized here so the parsers in fetch_pr / fetch_issue and
# the renderer in app/web.py stay in sync via a single source of truth.
_LIFECYCLE_TYPENAME_TO_ACTION = {
    "MergedEvent":         "merged",
    "ClosedEvent":         "closed",
    "ReopenedEvent":       "reopened",
    "ReadyForReviewEvent": "ready_for_review",
    "ConvertToDraftEvent": "converted_to_draft",
}


def _parse_lifecycle_events(nodes) -> list[dict]:
    """timelineItems nodes → list of REST-ish dicts ({id, action, actor,
    created_at, reason}). `reason` is the close-reason for Issue
    ClosedEvents (`completed` / `not_planned` / `duplicate`); PRs and
    other events return None there. The GraphQL global node `id` is
    the dedup key used as external_id when written to thread_events."""
    out: list[dict] = []
    for n in nodes or []:
        action = _LIFECYCLE_TYPENAME_TO_ACTION.get(n.get("__typename") or "")
        if not action:
            continue
        out.append({
            "id":         n.get("id"),
            "action":     action,
            "actor":      (n.get("actor") or {}).get("login"),
            "created_at": n.get("createdAt"),
            "reason":     ((n.get("stateReason") or "").lower() or None),
        })
    return out


# CheckRun.conclusion values that count as a failed check for triage — the
# branch-protection blockers plus the ones a human reads as "went wrong".
# NEUTRAL / SKIPPED / SUCCESS / STALE don't block (STALE means a newer run
# superseded it).
_CHECK_FAIL_CONCLUSIONS = frozenset({
    "FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE",
})


def _condense_check_rollup(rollup: dict | None) -> dict | None:
    """statusCheckRollup → {state, failing?, pending?} or None.

    `state` is GitHub's rollup verdict lowercased (success / failure / pending
    / error / expected); `failing` / `pending` are context-name lists, present
    only when non-empty. None when the repo runs no checks (rollup is null).
    Context names are capped at the GraphQL 100 — rarely hit; undercounts if so.
    """
    if not rollup:
        return None
    failing: list[str] = []
    pending: list[str] = []
    for ctx in (rollup.get("contexts") or {}).get("nodes") or []:
        if ctx.get("__typename") == "CheckRun":
            name = ctx.get("name")
            if (ctx.get("status") or "").upper() != "COMPLETED":
                pending.append(name)
            elif (ctx.get("conclusion") or "").upper() in _CHECK_FAIL_CONCLUSIONS:
                failing.append(name)
        else:  # StatusContext
            name = ctx.get("context")
            st = (ctx.get("state") or "").upper()
            if st in ("PENDING", "EXPECTED"):
                pending.append(name)
            elif st in ("FAILURE", "ERROR"):
                failing.append(name)
    out: dict = {"state": (rollup.get("state") or "").lower() or None}
    failing = [n for n in failing if n]
    pending = [n for n in pending if n]
    if failing:
        out["failing"] = failing
    if pending:
        out["pending"] = pending
    return out


# Per-comment body cap inside sampled threads — review threads and discussion
# reply threads come dozens at a time, unlike the item body's generous 32k
# guardrail. The *_BODY_CAP figures bound how many threads/comments actually
# carry sampled bodies; beyond that they're count-only (a PR/discussion that
# busy is already an obvious `look`).
_SAMPLED_COMMENT_TRUNC = 600
_REVIEW_THREAD_BODY_CAP = 12
_DISCUSSION_REPLY_BODY_CAP = 12


def _truncate_body(c: dict) -> str:
    body = (c.get("body") or "").strip()
    if len(body) > _SAMPLED_COMMENT_TRUNC:
        body = body[:_SAMPLED_COMMENT_TRUNC] + "…[truncated]"
    return body


def _review_thread_comment(c: dict) -> dict:
    """One sampled review-thread comment → {author, body}; body is raw
    markdown, truncated."""
    return {"author": (c.get("author") or {}).get("login"), "body": _truncate_body(c)}


def _sampled_reply(c: dict) -> dict:
    """One sampled discussion reply → {author, created_at, body}; body is
    raw markdown, truncated."""
    return {
        "author": (c.get("author") or {}).get("login"),
        "created_at": c.get("createdAt"),
        "body": _truncate_body(c),
    }


def _condense_review_threads(node) -> dict | None:
    """reviewThreads connection → {resolved: int, unresolved: [{path, comments,
    last_comment_at?, outdated?, comments_sample?}]} or None when the PR has no
    review threads.

    Resolved threads collapse to a bare count — resolved means that point was
    dealt with, no further detail needed. Unresolved ones carry their comment
    count + last-comment timestamp (which separates a live back-and-forth from
    a remark nobody ever marked resolved that's gone quiet), `outdated` when the
    diff moved out from under the thread, and — for the first
    _REVIEW_THREAD_BODY_CAP of them — `comments_sample`: the opening comment
    plus the last one or two, verbatim and truncated. The opener is what tells
    you *what* is being discussed; a bare tail usually doesn't ("what if it's
    xy.py?" / "I'll look Monday" / "ok" never restates the topic). When
    `comments` exceeds the sample length there's a gap between opener and tail —
    the count makes that plain. `unresolved` is omitted when every thread is
    resolved. Capped at the GraphQL 100; more threads undercount, as elsewhere.
    """
    if not node:
        return None
    resolved = 0
    unresolved: list[dict] = []
    bodied = 0
    for t in node.get("nodes") or []:
        if t.get("isResolved"):
            resolved += 1
            continue
        latest_nodes = (t.get("latest") or {}).get("nodes") or []
        total = (t.get("latest") or {}).get("totalCount") or len(latest_nodes)
        entry: dict = {"path": t.get("path"), "comments": total}
        last_at = latest_nodes[-1].get("createdAt") if latest_nodes else None
        if last_at:
            entry["last_comment_at"] = last_at
        if t.get("isOutdated"):
            entry["outdated"] = True
        if bodied < _REVIEW_THREAD_BODY_CAP:
            sample: list[dict] = []
            opener_nodes = (t.get("opener") or {}).get("nodes") or []
            # Prepend the opener only when it falls outside the latest window
            # (otherwise it's already the first of latest_nodes).
            if opener_nodes and total > len(latest_nodes):
                sample.append(_review_thread_comment(opener_nodes[0]))
            sample.extend(_review_thread_comment(c) for c in latest_nodes)
            if sample:
                entry["comments_sample"] = sample
                bodied += 1
        unresolved.append(entry)
    if resolved == 0 and not unresolved:
        return None
    out: dict = {"resolved": resolved}
    if unresolved:
        out["unresolved"] = unresolved
    return out


# Built-in Project field names whose values dupe data we already carry, so
# folding them into `fields` would just bloat the prompt. "Title" is the
# item's title (we have it on the row); the user / label / milestone / repo
# built-ins are skipped at the GraphQL level (we don't request those value
# types — see _PR_QUERY / _ISSUE_QUERY).
_PROJECT_FIELD_SKIPS = frozenset({"Title"})


def _project_field_value(node: dict) -> tuple[str, object] | None:
    """One ProjectV2ItemFieldValue node → (field_name, value) or None when
    the field is unsupported / empty / a built-in dupe."""
    fname = ((node.get("field") or {}) or {}).get("name")
    if not fname or fname in _PROJECT_FIELD_SKIPS:
        return None
    typename = node.get("__typename") or ""
    if typename == "ProjectV2ItemFieldSingleSelectValue":
        v = node.get("name")
    elif typename == "ProjectV2ItemFieldTextValue":
        v = node.get("text")
    elif typename == "ProjectV2ItemFieldNumberValue":
        v = node.get("number")
    elif typename == "ProjectV2ItemFieldDateValue":
        v = node.get("date")
    elif typename == "ProjectV2ItemFieldIterationValue":
        v = node.get("title")
    else:
        return None
    if v is None or v == "":
        return None
    return fname, v


def _parse_project_items(node) -> list[dict]:
    """projectItems connection → [{project, fields?}, …]. `project` is the
    board title; `fields` is a `{field_name: value}` dict of the board's
    custom fields (Status, Priority, Iteration, Estimate, …) — single-select
    options, text, numbers, dates, and iteration titles. Built-in dupes
    (Title, plus assignees/labels/milestone/repo/PR/reviewer values we don't
    request at the GraphQL level) are dropped. Closed (archived) projects
    are dropped too — leftover bookkeeping the user rarely cares about.
    Capped at 10 projects × 20 fields by the GraphQL query; threads on more
    boards / boards with more fields undercount, matching the rest of the
    module."""
    out: list[dict] = []
    for it in (node or {}).get("nodes") or []:
        proj = (it or {}).get("project") or {}
        title = proj.get("title")
        if not title or proj.get("closed"):
            continue
        entry: dict = {"project": title}
        fields: dict = {}
        for fv in ((it.get("fieldValues") or {}) or {}).get("nodes") or []:
            kv = _project_field_value(fv or {})
            if kv:
                fields[kv[0]] = kv[1]
        if fields:
            entry["fields"] = fields
        out.append(entry)
    return out


def fetch_pr(token: str, api_url: str | None) -> dict | None:
    """GraphQL fetch for a PR. Replaces four REST round trips with one.

    Returns a payload shaped like a REST PullRequest (so web.py reads the
    same field names from details_json) plus four bonus keys folded in
    while we already have the data:
        _pr_reactions       — REST-shaped reactions dict (per-emoji + total)
        _unique_commenters  — distinct issue-comment authors
        _unique_reviewers   — distinct review authors (excluding PENDING)
        _review_state       — 'approved' | 'changes_requested' | None

    Connection limits (100 comments / 100 reviews / 20 labels) cover the
    long tail; counts above the cap are undercounted, matching the
    Discussion path's behavior.
    """
    parsed = _parse_repo_url(api_url, "pulls")
    if parsed is None:
        return None
    owner, name, number = parsed
    headers = {**_auth_headers(token), "Content-Type": "application/json"}
    r = _session.post(
        _GRAPHQL_URL,
        headers=headers,
        json={
            "query": _PR_QUERY,
            "variables": {"owner": owner, "name": name, "number": number},
        },
        timeout=20,
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    payload = r.json()
    # Partial errors (e.g. a token without `read:project` failing on
    # projectItems) come with usable `data` alongside the `errors` list — log
    # and continue; the per-field parsers already handle null inputs. We only
    # bail when the core object itself is missing.
    if payload.get("errors"):
        log.warning(
            "GraphQL errors fetching PR %s/%s#%s: %s",
            owner, name, number, payload["errors"],
        )
    pr = ((payload.get("data") or {}).get("repository") or {}).get("pullRequest")
    if not pr:
        return None

    # Reactions: REST shape {emoji: count, total_count: n}.
    reactions: dict[str, int] = {v: 0 for v in _GRAPHQL_REACTION_KEYS.values()}
    rx_total = 0
    for g in pr.get("reactionGroups") or []:
        n = ((g.get("reactors") or {}).get("totalCount")) or 0
        key = _GRAPHQL_REACTION_KEYS.get(g.get("content"))
        if key is not None:
            reactions[key] = n
        rx_total += n
    reactions["total_count"] = rx_total

    # Commenters + comment history (chronological; the connection uses
    # last:100 so we keep the most-recent slice).
    comments_node = pr.get("comments") or {}
    comment_total = comments_node.get("totalCount") or 0
    commenter_logins: set[str] = set()
    comment_history: list[dict] = []
    for c in comments_node.get("nodes") or []:
        login = (c.get("author") or {}).get("login")
        if login:
            commenter_logins.add(login)
        comment_history.append({
            "database_id": c.get("databaseId"),
            "user": {"login": login},
            "author_association": c.get("authorAssociation"),
            "created_at": c.get("createdAt"),
            "edited_at": c.get("lastEditedAt"),
            "body": c.get("body") or "",
            **_comment_node_extras(c),
        })

    # Reviews — feed _compute_review_state in REST shape, count distinct
    # non-PENDING authors. Body and submittedAt ride along so the AI
    # summary can surface change-request rationales; comment_count (inline
    # line-comments) and reactions ride along too where present.
    rest_reviews: list[dict] = []
    reviewer_logins: set[str] = set()
    for rev in (pr.get("reviews") or {}).get("nodes") or []:
        state = rev.get("state")
        author_login = (rev.get("author") or {}).get("login")
        rev_entry: dict = {
            "database_id": rev.get("databaseId"),
            "state": state,
            "user": {"login": author_login},
            "author_association": rev.get("authorAssociation"),
            "submitted_at": rev.get("submittedAt"),
            "edited_at": rev.get("lastEditedAt"),
            "body": rev.get("body") or "",
        }
        rev_rx = _condense_reaction_groups(rev.get("reactionGroups"))
        if rev_rx:
            rev_entry["reactions"] = rev_rx
        rev_cc = ((rev.get("comments") or {}).get("totalCount")) or 0
        if rev_cc:
            rev_entry["comment_count"] = rev_cc
        rest_reviews.append(rev_entry)
        if state != "PENDING" and author_login:
            reviewer_logins.add(author_login)
    review_state = _compute_review_state(rest_reviews)

    # Assignees / requested reviewers / requested teams — REST shape.
    assignees = [
        {"login": (a or {}).get("login")}
        for a in (pr.get("assignees") or {}).get("nodes") or []
        if (a or {}).get("login")
    ]
    # Per-entry CODEOWNERS routing flag rides on the ReviewRequest itself
    # (scalar — survives org OAuth restrictions that null out the union below).
    # When the union resolves, attach the flag to the entry directly.
    # When the union is null'd (FORBIDDEN on restricted orgs), stash the flag
    # so the REST fallback below can apply it after recovering identities.
    requested_reviewers: list[dict] = []
    requested_teams: list[dict] = []
    unresolved_flags: list[bool] = []
    for rr in (pr.get("reviewRequests") or {}).get("nodes") or []:
        flag = bool(rr.get("asCodeOwner"))
        rev = rr.get("requestedReviewer") or {}
        if rev.get("__typename") == "User" and rev.get("login"):
            requested_reviewers.append({"login": rev["login"], "as_code_owner": flag})
        elif rev.get("__typename") == "Team" and rev.get("slug"):
            requested_teams.append({"slug": rev["slug"], "as_code_owner": flag})
        else:
            unresolved_flags.append(flag)

    if unresolved_flags:
        # GraphQL gave us N anonymous entries (identity blocked by the org's
        # OAuth-app restriction). REST /pulls/{n} isn't subject to the same
        # gate and returns the user / team identities directly. Merge them in,
        # attributing the as_code_owner flag uniformly when all unresolved
        # entries agree (the common case — CODEOWNERS routes everyone on a
        # path identically); otherwise mark them None to signal "can't tell".
        merged_flag: bool | None = (
            unresolved_flags[0] if len(set(unresolved_flags)) == 1 else None
        )
        known_logins = {(r or {}).get("login") for r in requested_reviewers}
        known_slugs = {(t or {}).get("slug") for t in requested_teams}
        try:
            rest = _session.get(
                f"https://api.github.com/repos/{owner}/{name}/pulls/{number}",
                headers=_auth_headers(token), timeout=15,
            )
            if rest.status_code == 200:
                rj = rest.json() or {}
                for u in rj.get("requested_reviewers") or []:
                    login = (u or {}).get("login")
                    if login and login not in known_logins:
                        requested_reviewers.append(
                            {"login": login, "as_code_owner": merged_flag}
                        )
                for t in rj.get("requested_teams") or []:
                    slug = (t or {}).get("slug")
                    if slug and slug not in known_slugs:
                        requested_teams.append(
                            {"slug": slug, "as_code_owner": merged_flag}
                        )
                log.info(
                    "PR %s/%s#%s: REST fallback recovered %d reviewer(s) / "
                    "%d team(s) hidden by GraphQL union restrictions",
                    owner, name, number,
                    len(rj.get("requested_reviewers") or []),
                    len(rj.get("requested_teams") or []),
                )
            else:
                log.warning(
                    "PR %s/%s#%s: REST fallback for review requests returned %s",
                    owner, name, number, rest.status_code,
                )
        except Exception:
            log.exception(
                "PR %s/%s#%s: REST fallback for review requests failed",
                owner, name, number,
            )

    labels = [
        {
            "name": (l or {}).get("name"),
            "color": (l or {}).get("color"),
            "description": (l or {}).get("description"),
        }
        for l in (pr.get("labels") or {}).get("nodes") or []
    ]

    # Issues this PR closes on merge — {number, title, state, reactions?}.
    # The linked issue's reaction count is a popularity proxy for the
    # underlying request: a PR closing a +200 issue carries more weight than
    # one closing a 0-reaction one.
    closes: list[dict] = []
    for cn in (pr.get("closingIssuesReferences") or {}).get("nodes") or []:
        num = (cn or {}).get("number")
        if not num:
            continue
        entry: dict = {
            "number": num,
            "title": (cn or {}).get("title"),
            "state": ((cn or {}).get("state") or "").lower() or None,
        }
        ci_rx = _condense_reaction_groups(cn.get("reactionGroups"))
        if ci_rx:
            entry["reactions"] = ci_rx
        closes.append(entry)

    # Per-file diff stats. REST-shaped (filename / additions / deletions) so
    # details_json stays consistent with the rest of this module's contract.
    # Capped at 100 by the GraphQL query; PRs with more changed files will
    # show a truncated list — the AI cross-references against `changed_files`
    # (the total count) to know it's not seeing everything.
    files = [
        {
            "filename": (f or {}).get("path"),
            "additions": (f or {}).get("additions"),
            "deletions": (f or {}).get("deletions"),
        }
        for f in (pr.get("files") or {}).get("nodes") or []
        if (f or {}).get("path")
    ]

    # Latest head commit — a cheap "when did the code last change" signal
    # that's distinct from updatedAt (which also bumps on comments / labels /
    # review requests). Kept in details_json (not a popped bonus key) so the
    # AI context builder and the UI can read it without a dedicated column.
    last_commit = None
    checks = None
    commits_node = pr.get("commits") or {}
    commit_nodes = commits_node.get("nodes") or []
    if commit_nodes:
        c = (commit_nodes[-1] or {}).get("commit") or {}
        if c.get("committedDate"):
            gh_author = c.get("author") or {}
            last_commit = {
                "abbrev_oid":   c.get("abbreviatedOid"),
                "message":      c.get("messageHeadline") or "",
                "committed_at": c.get("committedDate"),
                "author":       (gh_author.get("user") or {}).get("login") or gh_author.get("name"),
                "total":        commits_node.get("totalCount") or 0,
            }
        checks = _condense_check_rollup(c.get("statusCheckRollup"))

    # State: GraphQL OPEN/CLOSED/MERGED → REST 'open'/'closed'. _type_state
    # checks merged + draft *before* state, so a merged PR ends up correctly
    # classified regardless.
    gql_state = (pr.get("state") or "").lower()
    state = "closed" if gql_state == "merged" else gql_state

    # mergeStateStatus enum → REST mergeable_state string. _MERGE_STATE_DISPLAY
    # only acts on 'dirty' / 'unstable' / 'behind'; the rest fall through to
    # None and silently match REST's behavior on those values.
    merge_status = (pr.get("mergeStateStatus") or "").lower() or None

    author = pr.get("author") or {}
    return {
        "html_url": pr.get("url"),
        "created_at": pr.get("createdAt"),
        "updated_at": pr.get("updatedAt"),
        "body": pr.get("body"),
        # When the description was last edited (None if never) + who did it.
        # Distinct from updated_at, which also moves on comments / labels /
        # commits — _enrich fans this out into a `body_edit` thread_event.
        "body_edited_at": pr.get("lastEditedAt"),
        "body_editor": (pr.get("editor") or {}).get("login"),
        "state": state,
        "draft": pr.get("isDraft"),
        "merged": pr.get("merged"),
        "mergeable_state": merge_status,
        # GitHub's branch-protection-aware review verdict: 'approved' /
        # 'changes_requested' / 'review_required'; None when the repo doesn't
        # require reviews. Distinct from the _review_state we derive ourselves
        # (that's "what reviewers said", this is "does it satisfy the gate").
        "review_decision": (pr.get("reviewDecision") or "").lower() or None,
        # Condensed status-check rollup off the head commit: {state, failing?,
        # pending?} or None when no checks run. See _condense_check_rollup.
        "checks": checks,
        "additions": pr.get("additions"),
        "deletions": pr.get("deletions"),
        "changed_files": pr.get("changedFiles"),
        "files": files,
        "closes": closes,
        # GitHub Project (v2) boards this PR sits on, with each board's
        # custom fields ({Status, Priority, Iteration, …}). Advisory triage
        # context; field names / values are team-specific and the card can
        # lag the PR's state. See _parse_project_items.
        "projects": _parse_project_items(pr.get("projectItems")),
        # Inline review-thread state: {resolved: int, unresolved: [{path,
        # comments, last_comment_at?, outdated?, comments_sample?}]} or None.
        # See _condense_review_threads.
        "review_threads": _condense_review_threads(pr.get("reviewThreads")),
        "last_commit": last_commit,
        "comments": comment_total,
        "_comment_history": comment_history,
        "_reviews": rest_reviews,
        "_lifecycle_events": _parse_lifecycle_events(
            (pr.get("timelineItems") or {}).get("nodes")
        ),
        "author_association": pr.get("authorAssociation"),
        "user": {
            "login": author.get("login"),
            "avatar_url": author.get("avatarUrl"),
        },
        "assignees": assignees,
        "requested_reviewers": requested_reviewers,
        "requested_teams": requested_teams,
        "labels": labels,
        "_pr_reactions": reactions,
        "_unique_commenters": len(commenter_logins),
        "_unique_reviewers": len(reviewer_logins),
        "_review_state": review_state,
    }


_ISSUE_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      number
      title
      body
      lastEditedAt
      editor { login }
      url
      createdAt
      updatedAt
      state
      stateReason
      authorAssociation
      author { login avatarUrl }
      assignees(first: 10) { nodes { login } }
      labels(first: 20) { nodes { name color description } }
      reactionGroups { content reactors { totalCount } }
      projectItems(first: 10, includeArchived: false) {
        nodes {
          project { title closed }
          fieldValues(first: 20) {
            nodes {
              __typename
              ... on ProjectV2ItemFieldSingleSelectValue { name field { ... on ProjectV2FieldCommon { name } } }
              ... on ProjectV2ItemFieldTextValue         { text field { ... on ProjectV2FieldCommon { name } } }
              ... on ProjectV2ItemFieldNumberValue       { number field { ... on ProjectV2FieldCommon { name } } }
              ... on ProjectV2ItemFieldDateValue         { date field { ... on ProjectV2FieldCommon { name } } }
              ... on ProjectV2ItemFieldIterationValue    { title field { ... on ProjectV2FieldCommon { name } } }
            }
          }
        }
      }
      comments(last: 100) {
        totalCount
        nodes {
          databaseId
          author { login }
          authorAssociation
          body
          createdAt
          lastEditedAt
          isMinimized
          minimizedReason
          reactionGroups { content reactors { totalCount } }
        }
      }
      timelineItems(last: 50, itemTypes: [CLOSED_EVENT, REOPENED_EVENT]) {
        nodes {
          __typename
          ... on ClosedEvent   { id createdAt actor { login } stateReason }
          ... on ReopenedEvent { id createdAt actor { login } }
        }
      }
    }
  }
}
"""


def fetch_issue(token: str, api_url: str | None) -> dict | None:
    """GraphQL fetch for an Issue. Replaces the REST details + commenters
    pair with a single round trip.

    Returns REST-shaped payload (state / state_reason / reactions live
    inside details, so web.py reads the same field names from
    details_json) with one bonus key:
        _unique_commenters  — distinct comment authors
    """
    parsed = _parse_repo_url(api_url, "issues")
    if parsed is None:
        return None
    owner, name, number = parsed
    headers = {**_auth_headers(token), "Content-Type": "application/json"}
    r = _session.post(
        _GRAPHQL_URL,
        headers=headers,
        json={
            "query": _ISSUE_QUERY,
            "variables": {"owner": owner, "name": name, "number": number},
        },
        timeout=20,
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    payload = r.json()
    # See fetch_pr — partial errors are non-fatal as long as `data` carries
    # the core object.
    if payload.get("errors"):
        log.warning(
            "GraphQL errors fetching issue %s/%s#%s: %s",
            owner, name, number, payload["errors"],
        )
    issue = ((payload.get("data") or {}).get("repository") or {}).get("issue")
    if not issue:
        return None

    reactions: dict[str, int] = {v: 0 for v in _GRAPHQL_REACTION_KEYS.values()}
    rx_total = 0
    for g in issue.get("reactionGroups") or []:
        n = ((g.get("reactors") or {}).get("totalCount")) or 0
        key = _GRAPHQL_REACTION_KEYS.get(g.get("content"))
        if key is not None:
            reactions[key] = n
        rx_total += n
    reactions["total_count"] = rx_total

    comments_node = issue.get("comments") or {}
    comment_total = comments_node.get("totalCount") or 0
    commenter_logins: set[str] = set()
    comment_history: list[dict] = []
    for c in comments_node.get("nodes") or []:
        login = (c.get("author") or {}).get("login")
        if login:
            commenter_logins.add(login)
        comment_history.append({
            "database_id": c.get("databaseId"),
            "user": {"login": login},
            "author_association": c.get("authorAssociation"),
            "created_at": c.get("createdAt"),
            "edited_at": c.get("lastEditedAt"),
            "body": c.get("body") or "",
            **_comment_node_extras(c),
        })

    assignees = [
        {"login": (a or {}).get("login")}
        for a in (issue.get("assignees") or {}).get("nodes") or []
        if (a or {}).get("login")
    ]

    labels = [
        {
            "name": (l or {}).get("name"),
            "color": (l or {}).get("color"),
            "description": (l or {}).get("description"),
        }
        for l in (issue.get("labels") or {}).get("nodes") or []
    ]

    state = (issue.get("state") or "").lower()
    state_reason = issue.get("stateReason")
    state_reason = state_reason.lower() if state_reason else None

    author = issue.get("author") or {}
    return {
        "html_url": issue.get("url"),
        "created_at": issue.get("createdAt"),
        "updated_at": issue.get("updatedAt"),
        "body": issue.get("body"),
        "body_edited_at": issue.get("lastEditedAt"),
        "body_editor": (issue.get("editor") or {}).get("login"),
        "state": state,
        "state_reason": state_reason,
        "comments": comment_total,
        "_comment_history": comment_history,
        "_lifecycle_events": _parse_lifecycle_events(
            (issue.get("timelineItems") or {}).get("nodes")
        ),
        "author_association": issue.get("authorAssociation"),
        "user": {
            "login": author.get("login"),
            "avatar_url": author.get("avatarUrl"),
        },
        "assignees": assignees,
        "labels": labels,
        "reactions": reactions,
        # GitHub Project (v2) boards this issue sits on — see _parse_project_items.
        "projects": _parse_project_items(issue.get("projectItems")),
        "_unique_commenters": len(commenter_logins),
    }


def fetch_release(token: str, api_url: str | None) -> dict | None:
    """REST fetch for a Release. Returns a payload shaped to slot into the
    same details_json path as Issues (html_url / created_at / user /
    reactions) so popularity + age pills read it without branching, plus
    name / tag_name / body / prerelease / draft for the AI context.

    Releases use REST not GraphQL because the GraphQL release(tagName: …)
    lookup needs the tag name, and the notification subject only carries
    a numeric id.
    """
    if not api_url or not api_url.startswith("https://api.github.com/repos/"):
        return None
    r = _session.get(api_url, headers=_auth_headers(token), timeout=20)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    rel = r.json()
    user = rel.get("author") or {}
    return {
        "html_url": rel.get("html_url"),
        # published_at is the user-meaningful date; created_at can be days
        # earlier for releases drafted in advance. Falls back to created_at
        # for the rare case of a pre-publish notification.
        "created_at": rel.get("published_at") or rel.get("created_at"),
        "user": {
            "login": user.get("login"),
            "avatar_url": user.get("avatar_url"),
        },
        "reactions": rel.get("reactions"),
        "name": rel.get("name"),
        "tag_name": rel.get("tag_name"),
        "body": rel.get("body"),
        "prerelease": rel.get("prerelease"),
        "draft": rel.get("draft"),
    }


# Identity cache: per-user profile for AI credibility signal. The AI already
# sees login + authorAssociation per comment/review; this fills in the kind-
# of-person backdrop (account age, follower count, top repo, org/team role)
# that login alone doesn't carry. Refresh is lazy on AI judgment with a 7d
# TTL — at that cadence the steady state is ~zero extra HTTP per call.
USER_PROFILE_TTL_SECONDS = 7 * 24 * 3600

_USER_PROFILE_QUERY = """
query($login: String!) {
  user(login: $login) {
    bio
    company
    createdAt
    followers { totalCount }
  }
}
"""

# Combined query: profile + the org's teams the login is in. Saves a
# roundtrip vs. fetching them separately. `organization` may be null (org
# doesn't exist or isn't visible to our token); `teams(userLogins:)` only
# returns nodes when the *viewer* is in the org with permission to list
# teams — for external orgs it's a no-op (empty list), which is fine:
# per-comment `author_association` is the authoritative repo-level
# membership signal the AI relies on anyway.
_USER_PROFILE_WITH_ORG_QUERY = """
query($login: String!, $org: String!) {
  user(login: $login) {
    bio
    company
    createdAt
    followers { totalCount }
  }
  organization(login: $org) {
    teams(first: 50, userLogins: [$login]) {
      nodes { slug }
    }
  }
}
"""


def _truncate_bio(s: str | None) -> str | None:
    if not s:
        return None
    s = s.strip()
    if not s:
        return None
    return s if len(s) <= 100 else s[:97] + "…"


def _parse_user_profile(user_node: dict | None) -> dict | None:
    """Flatten a GraphQL `user` node into the column shape `people` stores.
    Returns None when the login is unknown (404 — caller writes a tombstone
    with fetched_at set so we don't re-fetch on every call). is_bot is
    derived caller-side from the login suffix, not from the GraphQL type:
    bots aren't resolvable via user(login:) at all, so this branch only
    runs for real Users."""
    if not user_node:
        return None
    return {
        "is_bot": 0,
        "bio": _truncate_bio(user_node.get("bio")),
        "company": (user_node.get("company") or None),
        "account_created_at": user_node.get("createdAt"),
        "followers": ((user_node.get("followers") or {}).get("totalCount") or 0),
    }


def _parse_org_membership(org_node: dict | None, login: str) -> dict:
    """Flatten the `organization.teams` block to a {teams: [slug, ...]}
    dict. Always returns a dict (empty teams when the org is missing or
    nothing is visible) so the caller always writes a row and avoids
    re-querying inside the TTL window."""
    if not org_node:
        return {"teams": []}
    teams = [
        (n.get("slug") or "")
        for n in ((org_node.get("teams") or {}).get("nodes") or [])
        if n and n.get("slug")
    ]
    return {"teams": teams}


def fetch_user_profile(
    token: str, login: str, org: str | None = None,
) -> tuple[dict | None, dict | None]:
    """One GraphQL roundtrip per call. Returns (profile, org_membership) —
    either may be None: profile is None when the login 404s; org_membership
    is None when no `org` was requested. When `org` is given, the membership
    dict is always returned (even if the login isn't in it), so the caller
    can persist a tombstone row and skip future calls for the TTL.
    """
    headers = {**_auth_headers(token), "Content-Type": "application/json"}
    if org:
        body = {
            "query": _USER_PROFILE_WITH_ORG_QUERY,
            "variables": {"login": login, "org": org},
        }
    else:
        body = {
            "query": _USER_PROFILE_QUERY,
            "variables": {"login": login},
        }
    r = _session.post(_GRAPHQL_URL, headers=headers, json=body, timeout=15)
    r.raise_for_status()
    data = (r.json() or {}).get("data") or {}
    profile = _parse_user_profile(data.get("user"))
    membership = _parse_org_membership(data.get("organization"), login) if org else None
    return profile, membership


def _user_is_fresh(conn: sqlite3.Connection, login: str, ttl: int) -> bool:
    row = conn.execute(
        "SELECT fetched_at FROM people WHERE login = ?", (login,),
    ).fetchone()
    if not row or row["fetched_at"] is None:
        return False
    return (int(time.time()) - int(row["fetched_at"])) < ttl


def _org_membership_is_fresh(
    conn: sqlite3.Connection, login: str, org: str, ttl: int,
) -> bool:
    row = conn.execute(
        "SELECT fetched_at FROM org_memberships WHERE login = ? AND org = ?",
        (login, org),
    ).fetchone()
    if not row:
        return False
    return (int(time.time()) - int(row["fetched_at"])) < ttl


def _upsert_user_profile(
    conn: sqlite3.Connection, login: str, profile: dict | None, now: int,
) -> None:
    """Persist a fetched profile. `profile is None` means the login 404'd —
    we still bump fetched_at to suppress re-fetches for the TTL window."""
    if profile is None:
        # Tombstone: just stamp fetched_at, leave other fields alone (the
        # row may not exist yet — `last_seen_at = NULL` is fine).
        conn.execute(
            "INSERT INTO people (login, fetched_at) VALUES (?, ?) "
            "ON CONFLICT(login) DO UPDATE SET fetched_at = excluded.fetched_at",
            (login, now),
        )
        return
    conn.execute(
        "INSERT INTO people (login, fetched_at, bio, company, "
        "  account_created_at, followers, is_bot) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(login) DO UPDATE SET "
        "  fetched_at         = excluded.fetched_at, "
        "  bio                = excluded.bio, "
        "  company            = excluded.company, "
        "  account_created_at = excluded.account_created_at, "
        "  followers          = excluded.followers, "
        "  is_bot             = excluded.is_bot",
        (login, now, profile["bio"], profile["company"],
         profile["account_created_at"], profile["followers"],
         profile["is_bot"]),
    )


def _upsert_org_membership(
    conn: sqlite3.Connection, login: str, org: str, membership: dict, now: int,
) -> None:
    conn.execute(
        "INSERT INTO org_memberships (login, org, teams_json, fetched_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(login, org) DO UPDATE SET "
        "  teams_json = excluded.teams_json, "
        "  fetched_at = excluded.fetched_at",
        (login, org,
         json.dumps(membership["teams"]) if membership.get("teams") else None,
         now),
    )


def _resolve_cached_token() -> str | None:
    """Token lookup for cache-fill paths that don't have one threaded in
    (the AI judge call chain). Matches auth.get_token's precedence —
    GITHUB_TOKEN env, then the stored OAuth token — but never triggers the
    interactive device flow. None means "no token available, skip the call"."""
    if t := os.environ.get("GITHUB_TOKEN"):
        return t.strip()
    return oauth.load_stored_token()


def _is_bot_login(login: str) -> bool:
    """GitHub renders bot logins with a `[bot]` suffix everywhere they
    surface in API responses — `dependabot[bot]`, `github-actions[bot]`,
    `renovate[bot]`. The GraphQL `user(login:)` query returns null for
    these because their type is `Bot`, not `User`, so we short-circuit
    rather than fetch and discard."""
    return login.endswith("[bot]")


def ensure_user_fresh(
    conn: sqlite3.Connection, login: str,
    *, token: str | None = None,
    ttl: int = USER_PROFILE_TTL_SECONDS,
) -> None:
    """No-op when the login is fresh inside the TTL. Otherwise fetches and
    upserts. `token` is optional: resolved from env / stored OAuth when
    omitted so AI callers don't have to plumb it through. Swallows network
    errors (logged) — a stale cache is better than a failed judgment."""
    if not login or _user_is_fresh(conn, login, ttl):
        return
    if _is_bot_login(login):
        # Bots aren't resolvable via user(login:) — skip the GraphQL hop
        # and stamp a minimal row so the AI sees is_bot=1 next time it
        # reads the cache, and the TTL suppresses the lookup.
        conn.execute(
            "INSERT INTO people (login, fetched_at, is_bot) VALUES (?, ?, 1) "
            "ON CONFLICT(login) DO UPDATE SET "
            "  fetched_at = excluded.fetched_at, "
            "  is_bot     = 1",
            (login, int(time.time())),
        )
        return
    tok = token or _resolve_cached_token()
    if not tok:
        return
    try:
        profile, _ = fetch_user_profile(tok, login, org=None)
    except (requests.RequestException, ValueError) as e:
        log.warning("ensure_user_fresh: %s (%s)", login, e)
        return
    _upsert_user_profile(conn, login, profile, int(time.time()))


def ensure_org_membership_fresh(
    conn: sqlite3.Connection, login: str, org: str,
    *, token: str | None = None,
    ttl: int = USER_PROFILE_TTL_SECONDS,
) -> None:
    """Combined fetch: refreshes the user profile too if it's stale, since
    `fetch_user_profile(..., org=...)` returns both in one roundtrip."""
    if not login or not org:
        return
    user_fresh = _user_is_fresh(conn, login, ttl)
    org_fresh = _org_membership_is_fresh(conn, login, org, ttl)
    if user_fresh and org_fresh:
        return
    # Bots aren't org members; skip the call but still stamp the tombstone
    # so we don't re-check every judgment. Both the login-suffix heuristic
    # and a cached is_bot row count.
    bot = _is_bot_login(login)
    if not bot:
        row = conn.execute(
            "SELECT is_bot FROM people WHERE login = ?", (login,),
        ).fetchone()
        bot = bool(row and row["is_bot"])
    if bot:
        # Ensure the people row exists too (with is_bot=1) so the AI side
        # doesn't try to render a stranger entry from a missing cache.
        ensure_user_fresh(conn, login, token=token, ttl=ttl)
        _upsert_org_membership(
            conn, login, org, {"teams": []}, int(time.time()),
        )
        return
    tok = token or _resolve_cached_token()
    if not tok:
        return
    try:
        profile, membership = fetch_user_profile(tok, login, org=org)
    except (requests.RequestException, ValueError) as e:
        log.warning("ensure_org_membership_fresh: %s @ %s (%s)", login, org, e)
        return
    now = int(time.time())
    if not user_fresh:
        _upsert_user_profile(conn, login, profile, now)
    if membership is not None:
        _upsert_org_membership(conn, login, org, membership, now)


# User-triage input fetch (separate path from the involved-people cache).
# Returns the full public-data payload for one login in a single GraphQL
# roundtrip, shaped for direct serialization into the user-triage prompt
# (see `app/ai_user_triage_prompt.md`). Generic per-login data only — no
# org context, no Eddy preferences, no repo filtering. The summary it
# feeds into is reused across every thread the login appears on.
_USER_TRIAGE_QUERY = """
query($login: String!) {
  user(login: $login) {
    name
    bio
    company
    location
    websiteUrl
    createdAt
    followers { totalCount }
    allOwned: repositories(first: 1, ownerAffiliations: OWNER) { totalCount }
    originalOwned: repositories(first: 1, ownerAffiliations: OWNER, isFork: false) { totalCount }
    repositoriesContributedTo(first: 1, includeUserRepositories: false,
      contributionTypes: [COMMIT, PULL_REQUEST, ISSUE]) { totalCount }
    pinnedItems(first: 6, types: [REPOSITORY]) {
      nodes { ... on Repository { name description primaryLanguage { name } stargazerCount } }
    }
    topRepos: repositories(first: 10, ownerAffiliations: OWNER, isFork: false,
                           orderBy: {field: STARGAZERS, direction: DESC}) {
      nodes { name description primaryLanguage { name } stargazerCount pushedAt }
    }
    contributionsCollection {
      contributionYears
      hasAnyContributions
    }
  }
}
"""


def _truncate_text(s: str | None, n: int) -> str | None:
    if not s:
        return None
    s = s.strip()
    if not s:
        return None
    return s if len(s) <= n else s[: n - 1] + "…"


def _flatten_repo_node(n: dict | None) -> dict | None:
    if not n or not n.get("name"):
        return None
    out: dict = {
        "name": n["name"],
        "stars": n.get("stargazerCount") or 0,
    }
    if (n.get("primaryLanguage") or {}).get("name"):
        out["language"] = n["primaryLanguage"]["name"]
    desc = _truncate_text(n.get("description"), 200)
    if desc:
        out["description"] = desc
    if n.get("pushedAt"):
        out["pushed_at"] = n["pushedAt"]
    return out


def fetch_user_triage_inputs(token: str, login: str) -> dict | None:
    """One GraphQL roundtrip → flat dict shaped for the user-triage prompt.
    Returns None when the login is unknown (404 / null user) — caller writes
    a tombstone so we don't refetch every call. Bot logins (`*[bot]`) aren't
    resolvable here and should be filtered upstream."""
    if _is_bot_login(login):
        return None
    headers = {**_auth_headers(token), "Content-Type": "application/json"}
    r = _session.post(
        _GRAPHQL_URL, headers=headers,
        json={"query": _USER_TRIAGE_QUERY, "variables": {"login": login}},
        timeout=20,
    )
    r.raise_for_status()
    data = (r.json() or {}).get("data") or {}
    u = data.get("user")
    if not u:
        return None
    out: dict = {"login": login}
    for k in ("name", "company", "location", "websiteUrl"):
        if u.get(k):
            out[k] = u[k]
    if u.get("bio"):
        out["bio"] = _truncate_text(u["bio"], 300)
    if u.get("createdAt"):
        out["account_created_at"] = u["createdAt"]
    out["followers"] = (u.get("followers") or {}).get("totalCount") or 0
    # owned_repos: total under their account (includes forks — a "fork
    # hoarder" pattern inflates this). original_owned_repos: non-fork
    # only — the meaningful "things they've actually built" count. The
    # gap between the two is the size of their fork collection.
    out["owned_repos"] = (u.get("allOwned") or {}).get("totalCount") or 0
    out["original_owned_repos"] = (u.get("originalOwned") or {}).get("totalCount") or 0
    out["contributed_to"] = (u.get("repositoriesContributedTo") or {}).get("totalCount") or 0
    cc = u.get("contributionsCollection") or {}
    # GitHub has no direct "is private profile" bool. Closest heuristic:
    # `hasAnyContributions=false` (no contributions in the calendar
    # window) despite at least one `top_repos[].pushed_at` inside that
    # same window. Pushes ARE contributions, so a blank calendar with
    # a live repo means visibility is restricted, not that the user is
    # inactive. Defaults to a 365-day cutoff to match GitHub's default
    # contributionsCollection window. When the heuristic fires, the
    # year list is dropped — it's not informative for private profiles
    # (GitHub returns the full account-lifetime year list regardless of
    # visibility, which misleads any reader taking it at face value).
    has_public = bool(cc.get("hasAnyContributions"))
    recent_push_cutoff = int(time.time()) - 365 * 86400
    raw_top_nodes = (u.get("topRepos") or {}).get("nodes") or []
    has_recent_push = any(
        (db.iso_to_unix((r or {}).get("pushedAt") or "") or 0) >= recent_push_cutoff
        for r in raw_top_nodes
    )
    profile_likely_private = (not has_public) and has_recent_push
    years = cc.get("contributionYears") or []
    if profile_likely_private:
        out["profile_likely_private"] = True
    elif years:
        # Ascending order reads more naturally in the prompt ("public
        # activity in 2013, 2014, 2018-...").
        out["contribution_years"] = sorted(years)
    pinned = [_flatten_repo_node(n) for n in ((u.get("pinnedItems") or {}).get("nodes") or [])]
    pinned = [p for p in pinned if p]
    if pinned:
        out["pinned"] = pinned
    top = [_flatten_repo_node(n) for n in ((u.get("topRepos") or {}).get("nodes") or [])]
    top = [p for p in top if p]
    if top:
        out["top_repos"] = top
    return out


# Org-triage input fetch. One GraphQL roundtrip, public-data-only.
# `membersWithRole.totalCount` is intentionally *not* requested — that
# field requires OAuth app approval on the org, and when forbidden it
# nullifies the entire parent `organization` field for the whole query
# (we hit this on OAuth-restricted orgs). The public-member count isn't
# worth losing the rest of the org data over; everything else here is
# accessible regardless of OAuth approval. Same reason `fundingLinks`
# is omitted on the repo side.
_ORG_TRIAGE_QUERY = """
query($org: String!) {
  organization(login: $org) {
    name
    description
    websiteUrl
    email
    location
    createdAt
    isVerified
    repositories { totalCount }
    topRepos: repositories(first: 10, isFork: false,
                           orderBy: {field: STARGAZERS, direction: DESC}) {
      nodes { name description primaryLanguage { name } stargazerCount pushedAt }
    }
  }
}
"""


def fetch_org_triage_inputs(token: str, org: str) -> dict | None:
    """One GraphQL roundtrip → flat dict shaped for the org-triage prompt.
    Returns None when the org doesn't exist or is invisible to the token."""
    if not org:
        return None
    headers = {**_auth_headers(token), "Content-Type": "application/json"}
    r = _session.post(
        _GRAPHQL_URL, headers=headers,
        json={"query": _ORG_TRIAGE_QUERY, "variables": {"org": org}},
        timeout=20,
    )
    r.raise_for_status()
    data = (r.json() or {}).get("data") or {}
    o = data.get("organization")
    if not o:
        return None
    out: dict = {"login": org}
    if o.get("name"):
        out["name"] = o["name"]
    if o.get("description"):
        out["description"] = _truncate_text(o["description"], 300)
    for k_src, k_out in (
        ("websiteUrl", "website_url"),
        ("location", "location"),
        ("email", "email"),
    ):
        if o.get(k_src):
            out[k_out] = o[k_src]
    if o.get("createdAt"):
        out["created_at"] = o["createdAt"]
    out["is_verified"] = bool(o.get("isVerified"))
    out["total_repos"] = (o.get("repositories") or {}).get("totalCount") or 0
    top = [_flatten_repo_node(n) for n in ((o.get("topRepos") or {}).get("nodes") or [])]
    top = [p for p in top if p]
    if top:
        out["top_repos"] = top
    return out


# Repo-triage input fetch. One roundtrip, public-data-only, returns a
# flat dict for the prompt. README + CONTRIBUTING bodies are fetched as
# Blob.text and truncated client-side — README for the elevator-pitch /
# domain signal, CONTRIBUTING for triage-actionable rules ("PRs require
# prior issue discussion", "in maintenance mode"). Filename variants
# aliased in one query; the parser picks the first non-null match.
# `fundingLinks` is intentionally omitted — its `url` field requires
# `public_repo` scope (see project memory `private_repo_scope`).
_REPO_TRIAGE_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    description
    homepageUrl
    createdAt
    pushedAt
    isArchived
    isFork
    hasIssuesEnabled
    hasDiscussionsEnabled
    stargazerCount
    forkCount
    issues(states: OPEN) { totalCount }
    pullRequests(states: OPEN) { totalCount }
    primaryLanguage { name }
    licenseInfo { spdxId name }
    codeOfConduct { name }
    repositoryTopics(first: 10) { nodes { topic { name } } }
    readme_md:    object(expression: "HEAD:README.md")    { ... on Blob { text } }
    readme_rst:   object(expression: "HEAD:README.rst")   { ... on Blob { text } }
    readme_plain: object(expression: "HEAD:README")       { ... on Blob { text } }
    contributing_md:     object(expression: "HEAD:CONTRIBUTING.md")          { ... on Blob { text } }
    contributing_rst:    object(expression: "HEAD:CONTRIBUTING.rst")         { ... on Blob { text } }
    contributing_github: object(expression: "HEAD:.github/CONTRIBUTING.md")  { ... on Blob { text } }
    defaultBranchRef {
      target { ... on Commit { history(first: 1) { totalCount } } }
    }
  }
}
"""


# Markdown badge / HTML-comment noise at the top of READMEs (build-status
# shields, sponsor banners, comment placeholders) eats truncation budget
# without adding signal. Strip the cheap-to-recognise forms.
_README_BADGE_RE = re.compile(r"\[?!\[[^\]]*\]\([^)]*\)\]?\([^)]*\)|!\[[^\]]*\]\([^)]*\)")
_README_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def _clean_readme(text: str | None, n: int) -> str | None:
    """Light-touch markdown cleanup: drop badge images + HTML comments,
    collapse blank-line runs, truncate. Keeps the prose without doing a
    real markdown parse — Haiku skims past minor noise fine, this is
    just about not eating the first 200 chars with shields.io URLs."""
    if not text:
        return None
    cleaned = _README_BADGE_RE.sub("", text)
    cleaned = _README_HTML_COMMENT_RE.sub("", cleaned)
    # Collapse 3+ consecutive newlines into 2.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    if not cleaned:
        return None
    return cleaned if len(cleaned) <= n else cleaned[: n - 1] + "…"


def fetch_repo_triage_inputs(
    token: str, owner: str, name: str,
) -> dict | None:
    """One GraphQL roundtrip → flat dict shaped for the repo-triage
    prompt. Returns None when the repo doesn't exist or is invisible."""
    if not owner or not name:
        return None
    headers = {**_auth_headers(token), "Content-Type": "application/json"}
    r = _session.post(
        _GRAPHQL_URL, headers=headers,
        json={"query": _REPO_TRIAGE_QUERY,
              "variables": {"owner": owner, "name": name}},
        timeout=20,
    )
    r.raise_for_status()
    data = (r.json() or {}).get("data") or {}
    rp = data.get("repository")
    if not rp:
        return None
    out: dict = {"full_name": f"{owner}/{name}"}
    if rp.get("description"):
        out["description"] = _truncate_text(rp["description"], 300)
    if rp.get("homepageUrl"):
        out["homepage_url"] = rp["homepageUrl"]
    if rp.get("createdAt"):
        out["created_at"] = rp["createdAt"]
    if rp.get("pushedAt"):
        out["pushed_at"] = rp["pushedAt"]
    out["is_archived"] = bool(rp.get("isArchived"))
    out["is_fork"] = bool(rp.get("isFork"))
    out["has_issues_enabled"] = bool(rp.get("hasIssuesEnabled"))
    out["has_discussions_enabled"] = bool(rp.get("hasDiscussionsEnabled"))
    out["stars"] = rp.get("stargazerCount") or 0
    out["forks"] = rp.get("forkCount") or 0
    out["open_issues"] = (rp.get("issues") or {}).get("totalCount") or 0
    out["open_prs"] = (rp.get("pullRequests") or {}).get("totalCount") or 0
    if (rp.get("primaryLanguage") or {}).get("name"):
        out["primary_language"] = rp["primaryLanguage"]["name"]
    license_info = rp.get("licenseInfo") or {}
    if license_info.get("spdxId") or license_info.get("name"):
        out["license"] = license_info.get("spdxId") or license_info.get("name")
    coc = rp.get("codeOfConduct") or {}
    if coc.get("name"):
        out["code_of_conduct"] = coc["name"]
    topics = [
        ((n.get("topic") or {}).get("name") or "")
        for n in ((rp.get("repositoryTopics") or {}).get("nodes") or [])
        if n and (n.get("topic") or {}).get("name")
    ]
    if topics:
        out["topics"] = topics
    # First non-null candidate wins for each of README + CONTRIBUTING.
    # README budget is larger (1200 chars) than CONTRIBUTING (800) —
    # README often has a richer elevator pitch; CONTRIBUTING rules tend
    # to be concise.
    for alias in ("readme_md", "readme_rst", "readme_plain"):
        blob = rp.get(alias) or {}
        text = blob.get("text")
        if text:
            cleaned = _clean_readme(text, 1200)
            if cleaned:
                out["readme"] = cleaned
            break
    for alias in ("contributing_md", "contributing_rst", "contributing_github"):
        blob = rp.get(alias) or {}
        text = blob.get("text")
        if text:
            cleaned = _clean_readme(text, 800)
            if cleaned:
                out["contributing"] = cleaned
            break
    commits = (((rp.get("defaultBranchRef") or {}).get("target") or {})
               .get("history") or {}).get("totalCount")
    if commits is not None:
        out["total_commits"] = commits
    return out


def _enrich(conn: sqlite3.Connection, token: str) -> dict[str, set[str]]:
    """Fetch full details for up to ENRICHMENT_PER_POLL notifications that need it.

    PR / Issue / Discussion go through GraphQL (one round trip each, with
    reactions / review state / commenter counts folded in). Release goes
    through REST since the GraphQL release lookup needs the tag name.

    Returns {thread_id: {new event kinds this poll}} for the rows it touched —
    'comment' / 'review' / 'lifecycle' (a thread_event with an external_id we
    hadn't recorded before) and 'code' (a PR head commit oid that differs from
    baseline_head_oid). Consumed by _apply_mute_filter; threads not enriched
    this poll (beyond the cap, or non-PR/Issue types) simply aren't in the map.
    """
    rows = conn.execute(
        """
        SELECT id, api_url, type, baseline_head_oid, details_json FROM notifications
        WHERE type IN ('PullRequest', 'Issue', 'Discussion', 'Release')
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

    new_kinds: dict[str, set[str]] = {}
    for row in rows:
        try:
            if row["type"] == "Discussion":
                details = fetch_discussion(token, row["api_url"])
            elif row["type"] == "PullRequest":
                details = fetch_pr(token, row["api_url"])
            elif row["type"] == "Release":
                details = fetch_release(token, row["api_url"])
            else:
                details = fetch_issue(token, row["api_url"])
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
            _mark_enriched(row["id"])
            continue

        # Pop the bonus keys so they don't leak into details_json (which is
        # supposed to be REST-shaped). Bonus keys live in dedicated columns
        # or, for the comment / review streams, get fanned out into
        # thread_events below.
        pr_reactions = details.pop("_pr_reactions", None)
        n_commenters = details.pop("_unique_commenters", None)
        n_reviewers = details.pop("_unique_reviewers", None)
        review_state = details.pop("_review_state", None)
        comment_history = details.pop("_comment_history", None) or []
        pr_reviews = details.pop("_reviews", None) or []
        lifecycle_events = details.pop("_lifecycle_events", None) or []

        # COALESCE captures baseline_comments on first enrichment so the
        # '+N new comments' indicator stays alive through Read actions and
        # only shifts when actual notification activity changes the count.
        # details_fetched_at is written at the *end* of the per-row block
        # below, not here, so the dirty predicate flips clean only after the
        # thread_events fan-out is done — otherwise wait_until_enriched
        # waiters could be released onto a half-built timeline.
        conn.execute(
            "UPDATE notifications SET details_json = ?, "
            "baseline_comments = COALESCE(baseline_comments, ?) "
            "WHERE id = ?",
            (json.dumps(details), details.get("comments") or 0, row["id"]),
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

        # Snapshot the comment / review / lifecycle events we already have so
        # the fan-out below can tell which arrived *this poll* — that's the
        # signal _apply_mute_filter needs to decide whether a re-delivery is
        # all-muted activity.
        nk: set[str] = set()
        existing_events = {
            (r["kind"], r["external_id"])
            for r in conn.execute(
                "SELECT kind, external_id FROM thread_events "
                "WHERE thread_id = ? AND kind IN ('comment', 'review', 'lifecycle', 'body_edit')",
                (row["id"],),
            )
        }

        # Fan comments + reviews out into thread_events for the stateful
        # AI integration. Idempotent on the GitHub databaseId — re-fetch
        # updates the payload (catches body edits) instead of appending
        # duplicates. Comments without a databaseId (rare; only when the
        # author's account is deleted) get skipped — no stable dedup key.
        for c in comment_history:
            db_id = c.get("database_id")
            if db_id is None:
                continue
            if ("comment", str(db_id)) not in existing_events:
                nk.add("comment")
            ts = db.iso_to_unix(c.get("created_at")) or now
            payload = {
                "author": (c.get("user") or {}).get("login"),
                "author_association": c.get("author_association"),
                "body": c.get("body") or "",
                "created_at": c.get("created_at"),
                "edited_at": c.get("edited_at"),
            }
            if c.get("reactions"):
                payload["reactions"] = c["reactions"]
            if c.get("minimized_reason"):
                payload["minimized_reason"] = c["minimized_reason"]
            # Discussion comments only: reply sub-thread tail rides along (a
            # re-fetch UPDATEs the payload, so later replies propagate — but
            # like a body edit it doesn't re-add the `comment` kind to `nk`).
            if c.get("reply_count"):
                payload["reply_count"] = c["reply_count"]
            if c.get("replies_sample"):
                payload["replies_sample"] = c["replies_sample"]
            db.write_thread_event(
                conn,
                thread_id=row["id"],
                ts=ts,
                kind="comment",
                source="github",
                external_id=str(db_id),
                payload=payload,
            )
        for rev in pr_reviews:
            db_id = rev.get("database_id")
            if db_id is None:
                continue
            if ("review", str(db_id)) not in existing_events:
                nk.add("review")
            ts = db.iso_to_unix(rev.get("submitted_at")) or now
            payload = {
                "author": (rev.get("user") or {}).get("login"),
                "author_association": rev.get("author_association"),
                "body": rev.get("body") or "",
                "state": rev.get("state"),
                "submitted_at": rev.get("submitted_at"),
                "edited_at": rev.get("edited_at"),
            }
            if rev.get("comment_count"):
                payload["comment_count"] = rev["comment_count"]
            if rev.get("reactions"):
                payload["reactions"] = rev["reactions"]
            db.write_thread_event(
                conn,
                thread_id=row["id"],
                ts=ts,
                kind="review",
                source="github",
                external_id=str(db_id),
                payload=payload,
            )
        # State transitions (merged / closed / reopened / draft↔ready).
        # Deduped on the GraphQL global node id; re-fetch leaves them
        # untouched (the events are immutable on GitHub's side once
        # they happen).
        for ev in lifecycle_events:
            ev_id = ev.get("id")
            if ev_id is None:
                continue
            if ("lifecycle", str(ev_id)) not in existing_events:
                nk.add("lifecycle")
            ts = db.iso_to_unix(ev.get("created_at")) or now
            payload = {
                "action": ev.get("action"),
                "actor":  ev.get("actor"),
            }
            if ev.get("reason"):
                payload["reason"] = ev["reason"]
            db.write_thread_event(
                conn,
                thread_id=row["id"],
                ts=ts,
                kind="lifecycle",
                source="github",
                external_id=str(ev_id),
                payload=payload,
            )

        # Description edits → a `body_edit` event keyed on the edit timestamp,
        # so each distinct edit appears once and a re-fetch is a no-op. Not a
        # muted kind (a re-delivery carrying one stays surfaced) and it counts
        # as new context for the AI — it only ever sees the *current* body,
        # never the diff, so an edit since its last verdict means "re-read the
        # body" (consumed via app/ai.py:_THINKING_REQUIRED_KINDS and
        # app/web.py:_VERDICT_INVALIDATING_KINDS). On first enrichment the edit
        # predates anything we've recorded — write it for the timeline, but
        # don't flag it as new-this-poll.
        body_edited_at = details.get("body_edited_at")
        if body_edited_at:
            if row["details_json"] and ("body_edit", body_edited_at) not in existing_events:
                nk.add("body_edit")
            db.write_thread_event(
                conn,
                thread_id=row["id"],
                ts=db.iso_to_unix(body_edited_at) or now,
                kind="body_edit",
                source="github",
                external_id=body_edited_at,
                payload={"editor": details.get("body_editor")},
            )

        # 'code' push: a PR head commit oid that differs from the one we last
        # saw. First enrichment (baseline_head_oid still NULL) captures the oid
        # without counting it as new — same shape as baseline_comments. When
        # the oid does shift, record a thread_event keyed on the new oid (so a
        # re-fetch of the same head is idempotent) carrying the diff totals at
        # this point in time — that's what lets a later judgment compare
        # "small fix" against the current `additions`/`deletions`/`changed_files`
        # and reframe scope without us hoarding per-file history.
        new_oid = None
        if row["type"] == "PullRequest":
            new_oid = (details.get("last_commit") or {}).get("abbrev_oid")
            if new_oid and row["baseline_head_oid"] and new_oid != row["baseline_head_oid"]:
                nk.add("code")
                lc = details.get("last_commit") or {}
                committed_at = lc.get("committed_at")
                payload = {
                    "oid":           new_oid,
                    "prev_oid":      row["baseline_head_oid"],
                    "committed_at":  committed_at,
                    "author":        lc.get("author"),
                    "additions":     details.get("additions"),
                    "deletions":     details.get("deletions"),
                    "changed_files": details.get("changed_files"),
                }
                db.write_thread_event(
                    conn,
                    thread_id=row["id"],
                    ts=db.iso_to_unix(committed_at) or now,
                    kind="code",
                    source="github",
                    external_id=new_oid,
                    payload={k: v for k, v in payload.items() if v is not None},
                )
        # Discussion close / reopen / answered isn't a GraphQL timeline event
        # (Discussion has no timelineItems) — detect it by diffing the `state`
        # we last stored against the fresh one, and synthesize a lifecycle
        # event so it shows in the timeline like a PR/Issue state change.
        if row["type"] == "Discussion" and row["details_json"]:
            try:
                old_state = (json.loads(row["details_json"]) or {}).get("state")
            except (ValueError, TypeError):
                old_state = None
            new_state = details.get("state")
            if old_state and new_state and old_state != new_state:
                nk.add("lifecycle")
                db.write_thread_event(
                    conn,
                    thread_id=row["id"],
                    ts=now,
                    kind="lifecycle",
                    source="github",
                    payload={"action": {"open": "reopened"}.get(new_state, new_state)},
                )
        if nk:
            new_kinds[row["id"]] = nk

        if row["type"] == "PullRequest":
            # All four bonus signals come from the same GraphQL call — write
            # them in one statement. COALESCE on baseline_review_state keeps
            # the first-seen review state pinned so the 'pill-new' dot only
            # fires when the state actually shifts after that; baseline_head_oid
            # instead advances on every fetch (it only feeds 'code' detection,
            # no persistent indicator hangs off it).
            conn.execute(
                "UPDATE notifications SET "
                "pr_reactions_json = ?, "
                "unique_commenters = ?, unique_reviewers = ?, "
                "pr_review_state = ?, "
                "baseline_review_state = COALESCE(baseline_review_state, ?), "
                "baseline_head_oid = COALESCE(?, baseline_head_oid) "
                "WHERE id = ?",
                (
                    json.dumps(pr_reactions) if pr_reactions is not None else None,
                    n_commenters,
                    n_reviewers,
                    review_state,
                    review_state,
                    new_oid,
                    row["id"],
                ),
            )
        elif n_commenters is not None:
            # Issues + Discussions: only bonus key is the commenter count.
            conn.execute(
                "UPDATE notifications SET unique_commenters = ? WHERE id = ?",
                (n_commenters, row["id"]),
            )

        # Self-authored activity counts as engagement: if the *latest* comment
        # / review / body_edit visible right now is the user's own, re-anchor
        # the since-visit baselines. Runs after the per-type baseline writers
        # above so the overwrite wins on first enrichment too (the COALESCE
        # baseline-setters would otherwise pin baselines to old state).
        if _latest_event_self_authored(
            comment_history, pr_reviews, body_edited_at, details.get("body_editor")
        ):
            _clear_engagement_baselines(conn, row["id"])

        # Last write in the per-row block: stamps the dirty-predicate clean.
        # Pair with _mark_enriched so any wait_until_enriched callers wake up
        # to a fully-populated thread_events timeline, not a half-built one.
        conn.execute(
            "UPDATE notifications SET details_fetched_at = ? WHERE id = ?",
            (now, row["id"]),
        )
        _mark_enriched(row["id"])

    return new_kinds


def _latest_event_self_authored(
    comment_history: list[dict],
    pr_reviews: list[dict],
    body_edited_at: str | None,
    body_editor: str | None,
) -> bool:
    """True iff the newest among comments / reviews / body-edit visible on
    the thread is authored by the authenticated user. Used to treat a fresh
    self-comment (in our app or anywhere on github.com) as an engagement
    event. A self-comment buried in the middle of a batch doesn't trigger:
    if someone else has the last word, there *is* new third-party activity
    worth surfacing in the pill."""
    if not _USER_LOGIN:
        return False
    candidates: list[tuple[int, str | None]] = []
    for c in comment_history:
        ts = db.iso_to_unix(c.get("created_at"))
        if ts is not None:
            candidates.append((ts, (c.get("user") or {}).get("login")))
    for r in pr_reviews:
        ts = db.iso_to_unix(r.get("submitted_at"))
        if ts is not None:
            candidates.append((ts, (r.get("user") or {}).get("login")))
    if body_edited_at:
        ts = db.iso_to_unix(body_edited_at)
        if ts is not None:
            candidates.append((ts, body_editor))
    if not candidates:
        return False
    candidates.sort(key=lambda c: c[0], reverse=True)
    return candidates[0][1] == _USER_LOGIN


def set_subscribed(token: str, thread_id: str) -> None:
    """Re-subscribe to a thread (reverse of set_ignored)."""
    if _is_synthetic(thread_id):
        return
    r = _session.put(
        f"{_thread_url(thread_id)}/subscription",
        headers=_auth_headers(token),
        json={"subscribed": True, "ignored": False},
        timeout=10,
    )
    if r.status_code == 200:
        return
    r.raise_for_status()


def _upsert(
    conn: sqlite3.Connection,
    item: dict[str, Any],
    now: int,
    touched: list | None = None,
) -> None:
    """Insert/update one notification row. If `touched` is given and the row
    existed before, append a (thread_id, prev_unread, prev_action, prev_ignored,
    prev_effective_updated_at, prev_snooze_until) tuple to it — the post-poll
    passes need the pre-delivery state: _apply_mute_filter to decide whether
    to re-suppress an all-muted re-delivery (and restore a snooze deadline
    intact), _apply_throttle to roll effective_updated_at back to the frozen
    front-fire value inside an active quiet-bystanders window."""
    subject = item.get("subject") or {}
    repo = item.get("repository") or {}
    reason = item.get("reason") or ""
    # link_url lifecycle: replace whenever the API gives us a new
    # latest_comment_url; otherwise preserve the previous capture (so the
    # link survives read state and only shifts when fresh activity arrives).
    # Mirrors the "indicators persist across user actions" rule.
    link_candidate = derive_link_url(item)
    # Detect a "marked read on GitHub" event before the upsert overwrites
    # the prior unread state. Our local mark-read flow updates the row's
    # unread column before the next poll runs, so a 1→0 transition observed
    # HERE must have come from outside the app — the user clicking the
    # notification on github.com, the notifications-feed auto-clear, etc.
    # We don't know whether they actually opened the underlying page or
    # just dismissed the notification, so the action label reflects that
    # uncertainty (distinct from the local 'visited' / 'read' labels).
    prev = conn.execute(
        "SELECT unread, action, ignored, muted_kinds, effective_updated_at, "
        "snooze_until, updated_at, last_read_at FROM notifications WHERE id = ?",
        (item["id"],),
    ).fetchone()
    if touched is not None and prev is not None:
        touched.append((
            item["id"], prev["unread"], prev["action"], prev["ignored"],
            prev["effective_updated_at"], prev["snooze_until"],
        ))
    new_unread = 1 if item.get("unread") else 0
    # Detect a github-side read by watching `last_read_at` advance rather
    # than the local `unread` flag's 1→0 transition. The flag-only trigger
    # missed: rows arriving already-read (no prior local state at all), and
    # bump-then-read-between-polls (prev.unread was already 0 from an
    # earlier in-app mark-read, then activity bumped + user read on github
    # — our `prev` never saw the unread=1 intermediate). `last_read_at`
    # advancing covers all three cases uniformly.
    prev_read_ts = db.iso_to_unix(prev["last_read_at"] or "") if prev else 0
    new_read_ts = db.iso_to_unix(item.get("last_read_at") or "") or 0
    read_advanced = new_read_ts > (prev_read_ts or 0)
    updated_at = item.get("updated_at") or ""
    # Resurface a locally-archived thread — Done (action='done') or Snooze
    # (action='snoozed') — when GitHub hands us the notification again *with new
    # activity*: both archive on GitHub, so a bumped updated_at means genuinely
    # new activity landed (this mirrors github.com's own "Done" auto-reset, and
    # beats a pending snooze timer). The updated_at gate matters because the
    # poll's unread fetch (a thread whose mark_done didn't take) and an explicit
    # backfetch (unconditional `?all=true` pull) can both re-deliver a hidden
    # row with nothing actually changed — without the gate that would spuriously
    # un-archive it / wipe its snooze deadline. Mute (action='done' + ignored)
    # stays archived regardless: the unsubscribe means GitHub won't deliver
    # further activity, and the point of Mute is "I never want to see this again".
    resurfaced = (
        prev is not None
        and prev["action"] in ("done", "snoozed")
        and not prev["ignored"]
        and updated_at != (prev["updated_at"] or "")
    )
    conn.execute(
        """
        INSERT INTO notifications (
            id, repo, type, title, reason, api_url, html_url,
            updated_at, last_read_at, unread, raw_json, fetched_at, link_url,
            effective_updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            fetched_at=excluded.fetched_at,
            link_url = COALESCE(excluded.link_url, notifications.link_url),
            effective_updated_at = CASE
                WHEN excluded.updated_at != notifications.updated_at
                    THEN excluded.updated_at
                ELSE notifications.effective_updated_at
            END
        """,
        (
            item["id"],
            repo.get("full_name") or "",
            subject.get("type") or "",
            subject.get("title") or "",
            reason,
            subject.get("url"),
            derive_html_url(item),
            updated_at,
            item.get("last_read_at"),
            new_unread,
            json.dumps(item),
            now,
            link_candidate,
            updated_at,
        ),
    )
    # Mark the enrichment signal dirty whenever the row picked up fresh
    # activity (brand-new row, or updated_at advanced). _enrich will _mark_
    # enriched() at the end of its per-row block to release waiters. Pure
    # read-state changes (unread flip with no updated_at move) leave the
    # signal alone — no new context to enrich.
    if prev is None or (prev["updated_at"] or "") != updated_at:
        _mark_dirty(item["id"])
    if reason:
        _accumulate_seen_reason(conn, item["id"], reason)
    if reason in ("mention", "team_mention") and updated_at:
        # One mention event per notification update where the delivery reason
        # is mention / team_mention. external_id = updated_at dedups within
        # a single re-delivery; a fresh poll with the same updated_at is a
        # no-op (the dedup index UPDATEs the payload in place). Consumed by
        # the pill ("+ mentioned" when this event's ts is newer than the
        # latest engagement) and by the AI timeline.
        db.write_thread_event(
            conn,
            thread_id=item["id"],
            ts=db.iso_to_unix(updated_at) or now,
            kind="mention",
            source="github",
            external_id=updated_at,
            payload={"reason": reason},
        )
    if read_advanced:
        # Use github's own `last_read_at` as the event ts — it's the actual
        # moment of engagement, which may be well before `now` (the user
        # read days ago and we're only now polling). A "Mark all as read"
        # sweep on github.com would trigger this too; weak signal, but
        # better than missing the engagement entirely.
        db.write_thread_event(
            conn,
            thread_id=item["id"],
            ts=new_read_ts,
            kind="user_action",
            source="github",
            payload={"action": "read_on_github"},
        )
        # Re-anchor the since-visit deltas so the pill stops counting from
        # before this read. Skipped on a brand-new row (prev is None):
        # details_json isn't populated yet, so the baseline would zero
        # against nothing; _enrich's first pass will set baseline_comments
        # to the current count on COALESCE, which gives the same effect.
        if prev is not None:
            _clear_engagement_baselines(conn, item["id"])
    if resurfaced:
        # New activity arrived on a Done/Snoozed thread — bring it back. No
        # thread_event marker for it: whatever triggered the re-delivery is
        # already in the timeline (a comment / review / lifecycle event), and
        # `action` going NULL is the state record. A *user*-triggered
        # un-archive is the separate `_apply_action` path, which logs `undone`.
        conn.execute(
            "UPDATE notifications SET action = NULL, actioned_at = NULL, "
            "action_source = NULL, snooze_until = NULL WHERE id = ?",
            (item["id"],),
        )
    # A genuine notification just arrived for a thread that a search-backfill
    # had stood in for (id "q_<node_id>") — drop the synthetic row (and its
    # timeline); the real row will re-enrich fresh, owning the thread from here.
    if prev is None and not item["id"].startswith("q_"):
        html = derive_html_url(item)
        if html:
            for r in conn.execute(
                "SELECT id FROM notifications "
                "WHERE id LIKE 'q\\_%' ESCAPE '\\' AND html_url = ? AND id != ?",
                (html, item["id"]),
            ).fetchall():
                conn.execute(
                    "DELETE FROM thread_events WHERE thread_id = ?", (r["id"],)
                )
                conn.execute("DELETE FROM notifications WHERE id = ?", (r["id"],))


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


def _clear_engagement_baselines(
    conn: sqlite3.Connection, thread_id: str
) -> None:
    """Snapshot current activity counters into baselines so post-engagement
    deltas surface as fresh signals. Called when the user demonstrably looked
    at the thread: in-app visit, observed read-on-github, or self-authored
    comment / review / body_edit. Reads details.comments from details_json
    (0 if not yet enriched); pr_review_state from its own column.

    The "since-visit" mention signal is event-sourced separately — a `mention`
    thread_event lands per re-delivery with reason in (mention, team_mention),
    and the row builder compares its ts against the latest engagement event
    in thread_events. No column to flip here."""
    row = conn.execute(
        "SELECT details_json, pr_review_state FROM notifications WHERE id = ?",
        (thread_id,),
    ).fetchone()
    if not row:
        return
    try:
        comments = (json.loads(row["details_json"] or "{}") or {}).get("comments") or 0
    except (ValueError, TypeError):
        comments = 0
    conn.execute(
        "UPDATE notifications SET baseline_comments = ?, "
        "baseline_review_state = ? WHERE id = ?",
        (comments, row["pr_review_state"], thread_id),
    )


# Per-thread notification-kind filter taxonomy. MUTE_KINDS is the set of
# *mutable* activity kinds (the keys allowed in notifications.muted_kinds JSON);
# MUTE_KINDS_BY_TYPE maps a notification's `type` to the subset that applies to
# it (an empty subset → the row has no filter UI and the AI gets no
# subscription tokens). `lifecycle` (merge / close / reopen / answered) is
# deliberately NOT mutable — it's the low-volume, high-signal outcome a watcher
# wants; it still surfaces (as a thread_event) and always notifies, but can't
# be silenced per-thread (want-nothing → unsubscribe). It shows in the ▾ menu
# as a disabled row. The keys of MUTE_KINDS_BY_TYPE are exactly the types that
# produce lifecycle events. Lives here because _enrich produces these kinds;
# web.py (UI) and ai.py (verdict tokens) import them.
MUTE_KINDS = ("comment", "review", "code")
MUTE_KINDS_BY_TYPE = {
    "PullRequest": ("comment", "review", "code"),
    "Issue":       ("comment",),
    "Discussion":  ("comment",),
}

# Notification `reason` values that mean "this is aimed at you" rather than a
# watch side effect — a re-delivery carrying one of these surfaces even if its
# activity kind is muted on the thread.
_DIRECTED_REASONS = {"mention", "team_mention", "assign", "review_requested"}

# Reasons that mean the user is *involved* in the thread (authored it, has
# commented on it, manually subscribed, or it's tied to their own activity).
# Like _DIRECTED_REASONS these bypass the quiet-bystanders throttle: an
# engaged user wants updates promptly. (Does NOT bypass per-kind mute — that's
# an explicit "I don't want this kind here" override.)
_INVOLVED_REASONS = {"author", "comment", "manual", "your_activity"}

# Union of the two: every `reason` that means "you, specifically" — directed
# at you or you're an active participant — as opposed to a passive repo/thread
# watch (`subscribed`, `ci_activity`, `state_change`, `push`, `security_alert`,
# …). Drives the "Involved only" row filter in the web UI.
INVOLVED_REASONS = _DIRECTED_REASONS | _INVOLVED_REASONS

# Activity kinds the bystander throttle is allowed to suppress. `review` and
# `lifecycle` always bump (lower-volume, higher-signal — the merge / approval /
# close ping you want promptly).
_THROTTLE_KINDS = {"comment", "code"}

# Quiet window after a bystander front-fire. One number, no UI picker — start
# fixed, only split into a Short/Long preset if 30 min ever feels wrong.
THROTTLE_WINDOW_SECONDS = 30 * 60


def _quiet_bystanders_enabled(conn: sqlite3.Connection) -> bool:
    """Whether the bystander-throttle toggle is on. Setting lives in
    config/settings.toml; conn arg kept for call-site compatibility."""
    return settings.get("quiet_bystanders")


def drain_throttle_windows(conn: sqlite3.Connection) -> None:
    """Clear every pending throttle window — bumping any row that has
    accumulated suppressed activity up to its true updated_at. Called from
    the web layer when the user flips the toggle off, so the effect of
    disabling is immediate (no waiting for in-flight windows to expire)."""
    conn.execute(
        "UPDATE notifications "
        "SET effective_updated_at = updated_at "
        "WHERE throttle_until IS NOT NULL AND updated_at > effective_updated_at"
    )
    conn.execute("UPDATE notifications SET throttle_until = NULL WHERE throttle_until IS NOT NULL")


def _apply_mute_filter(
    conn: sqlite3.Connection,
    token: str,
    touched: list,
    new_kinds: dict[str, set[str]],
) -> None:
    """For threads re-delivered this poll whose new activity is *entirely* of
    kinds muted on that thread (and not a directed-reason delivery), re-apply
    the thread's pre-delivery state and fold the activity into the baselines —
    so the row neither resurfaces nor lights up a "+N new" indicator, and keeps
    its sort slot instead of jumping to the top.

    `touched` is the (thread_id, prev_unread, prev_action, prev_ignored,
    prev_effective_updated_at, prev_snooze_until) tuples _upsert collected for
    every re-delivered row; rows without a non-empty muted_kinds short-circuit
    below. `new_kinds` is _enrich's per-thread new-kind map.
    """
    for (thread_id, prev_unread, prev_action, prev_ignored, prev_eff,
         prev_snooze) in touched:
        nk = new_kinds.get(thread_id)
        if not nk:
            continue  # no detectable new activity — surface normally (safe default)
        row = conn.execute(
            "SELECT reason, unread, muted_kinds, details_json, pr_review_state "
            "FROM notifications WHERE id = ?",
            (thread_id,),
        ).fetchone()
        if row is None:
            continue
        try:
            muted = set(json.loads(row["muted_kinds"] or "[]"))
        except (ValueError, TypeError):
            muted = set()
        if not muted:
            continue
        if (row["reason"] or "") in _DIRECTED_REASONS:
            continue  # directed at the user — let it surface
        if not nk.issubset(muted):
            continue  # some new activity isn't muted — surface normally

        # Absorb. Re-apply whichever state the thread was in pre-delivery.
        # A GitHub call here failing shouldn't abort the rest of the poll —
        # mirrors _enrich's per-row guard.
        try:
            now = int(time.time())
            if prev_action in ("done", "snoozed") and not prev_ignored:
                # _upsert just resurfaced it (action→NULL, snooze cleared) —
                # undo: archive on GitHub again and restore the prior hidden
                # state. A snoozed thread keeps its original deadline, so
                # absorbed activity can't push it past the wake time or
                # downgrade it to a plain Done.
                mark_done(token, thread_id)
                if prev_action == "snoozed":
                    conn.execute(
                        "UPDATE notifications SET action = 'snoozed', actioned_at = ?, "
                        "action_source = 'auto', snooze_until = ? WHERE id = ?",
                        (now, prev_snooze, thread_id),
                    )
                else:
                    conn.execute(
                        "UPDATE notifications SET action = 'done', actioned_at = ?, "
                        "action_source = 'auto', snooze_until = NULL WHERE id = ?",
                        (now, thread_id),
                    )
            elif not prev_unread and row["unread"]:
                # Was read; the re-delivery flipped it unread — re-mark read.
                mark_read(token, thread_id)
                conn.execute(
                    "UPDATE notifications SET unread = 0 WHERE id = ?", (thread_id,)
                )
            # (else: it was already unread and stays unread — only the sort
            #  rollback below applies, so the row doesn't bubble up.)

            try:
                comments = (json.loads(row["details_json"] or "{}") or {}).get("comments") or 0
            except (ValueError, TypeError):
                comments = 0
            conn.execute(
                "UPDATE notifications SET effective_updated_at = COALESCE(?, updated_at), "
                "baseline_comments = ?, baseline_review_state = ? WHERE id = ?",
                (prev_eff, comments, row["pr_review_state"], thread_id),
            )
            db.write_thread_event(
                conn,
                thread_id=thread_id,
                ts=now,
                kind="user_action",
                source="github",
                payload={"action": "absorbed", "kinds": sorted(nk)},
            )
        except Exception:
            log.exception("mute-filter absorb failed for %s", thread_id)


def _apply_throttle(
    conn: sqlite3.Connection,
    touched: list,
    new_kinds: dict[str, set[str]],
) -> None:
    """Quiet-bystanders throttle. For each re-delivered row whose new activity
    is entirely `comment` / `code` AND the thread is a bystander (not directed
    at the user, not involved, not tracked, not archived/snoozed):
      - If no active window (or it's expired): front-fire — leave the row's
        sort key advanced (as _upsert did) and open a new window of
        THROTTLE_WINDOW_SECONDS.
      - If a window is still active: roll effective_updated_at back to the
        pre-delivery value (held at the front-fire moment) so the row keeps
        its slot. Don't reset the window — the timer runs from the front-fire,
        not from each suppressed delivery.
    A non-throttled bump (directed/involved/review/lifecycle/etc.) clears any
    stale window so the next quiet burst starts a fresh one.
    """
    if not _quiet_bystanders_enabled(conn):
        return
    now = int(time.time())
    window_end = now + THROTTLE_WINDOW_SECONDS
    for (thread_id, _pu, _pa, _pi, prev_eff, _ps) in touched:
        nk = new_kinds.get(thread_id)
        if not nk:
            continue  # no new activity detected this poll
        row = conn.execute(
            "SELECT reason, action, is_tracked, muted_kinds, throttle_until "
            "FROM notifications WHERE id = ?",
            (thread_id,),
        ).fetchone()
        if row is None:
            continue
        try:
            muted = set(json.loads(row["muted_kinds"] or "[]"))
        except (ValueError, TypeError):
            muted = set()
        reason = row["reason"] or ""
        # All-muted re-deliveries were already handled by _apply_mute_filter
        # (which froze effective_updated_at to prev_eff and re-applied the
        # archived/read state). Nothing left for throttle to do.
        absorbed = (nk.issubset(muted) and reason not in _DIRECTED_REASONS)
        eligible = (
            not absorbed
            and row["action"] is None
            and not row["is_tracked"]
            and reason not in _DIRECTED_REASONS
            and reason not in _INVOLVED_REASONS
            and nk.issubset(_THROTTLE_KINDS)
        )
        if not eligible:
            # A real bump — drop any stale window so the next quiet burst on
            # this thread starts fresh.
            if row["throttle_until"] is not None:
                conn.execute(
                    "UPDATE notifications SET throttle_until = NULL WHERE id = ?",
                    (thread_id,),
                )
            continue
        tu = row["throttle_until"]
        if tu is None or tu <= now:
            # Front fire: _upsert already set effective_updated_at to the new
            # updated_at. Open a window so the next burst rides it out.
            conn.execute(
                "UPDATE notifications SET throttle_until = ? WHERE id = ?",
                (window_end, thread_id),
            )
        else:
            # Inside the window: roll the sort key back to where the front
            # fire left it (prev_eff carries that value forward poll-to-poll,
            # since each suppression re-pins effective_updated_at to it).
            conn.execute(
                "UPDATE notifications SET effective_updated_at = COALESCE(?, updated_at) "
                "WHERE id = ?",
                (prev_eff, thread_id),
            )


def _release_throttled(conn: sqlite3.Connection) -> None:
    """Trailing-edge sweep: any throttle window whose timer has expired gets
    closed. If activity was suppressed inside it (updated_at advanced past
    the frozen effective_updated_at), the row bumps now — the single
    "here's what happened" surface the throttle was building toward. Windows
    that expired with nothing suppressed just get cleared. Runs every poll
    independent of the toggle so windows always drain."""
    now = int(time.time())
    rows = conn.execute(
        "SELECT id, updated_at, effective_updated_at FROM notifications "
        "WHERE throttle_until IS NOT NULL AND throttle_until <= ?",
        (now,),
    ).fetchall()
    for row in rows:
        if (row["updated_at"] or "") > (row["effective_updated_at"] or ""):
            conn.execute(
                "UPDATE notifications SET effective_updated_at = updated_at, "
                "throttle_until = NULL WHERE id = ?",
                (row["id"],),
            )
        else:
            conn.execute(
                "UPDATE notifications SET throttle_until = NULL WHERE id = ?",
                (row["id"],),
            )


def _get_paginated(
    token: str,
    params: dict,
    last_modified: str | None,
    max_pages: int = MAX_PAGES_PER_FETCH,
    use_cache: bool = True,
) -> tuple[list[dict[str, Any]], str | None, int, bool]:
    """GET /notifications with optional If-Modified-Since + page cap.
    Returns (items, new_last_modified, status, truncated). status=304 means
    no changes; truncated is True when the page cap was reached AND the last
    response still advertised a `next` Link — callers that rely on
    "missing-from-response → mark read" reconciliation need it to know
    whether their "missing" set is authoritative.
    Pass use_cache=False to skip the conditional header — needed when the
    caller wants a guaranteed re-pull even if the feed's Last-Modified
    matches the bookmark (manual refresh repairing a local desync)."""
    headers = _auth_headers(token)
    if last_modified and use_cache:
        headers["If-Modified-Since"] = last_modified
    r = _session.get(API_NOTIFICATIONS, headers=headers, params=params, timeout=30)
    if r.status_code == 304:
        return [], last_modified, 304, False
    r.raise_for_status()
    new_last_modified = r.headers.get("Last-Modified", last_modified)
    items: list[dict[str, Any]] = list(r.json())
    page_headers = _auth_headers(token)  # no If-Modified-Since on subsequent pages
    pages = 1
    while "next" in r.links and pages < max_pages:
        r = _session.get(r.links["next"]["url"], headers=page_headers, timeout=30)
        r.raise_for_status()
        items.extend(r.json())
        pages += 1
    truncated = "next" in r.links and pages >= max_pages
    return items, new_last_modified, 200, truncated


def _fetch_unread(
    conn: sqlite3.Connection, token: str, touched: list | None = None,
    force: bool = False,
) -> int:
    """Fetch currently-unread notifications + reconcile read-state of items
    missing from the response (they were marked read elsewhere). This is the
    only path that catches read-state changes on threads with no new activity
    (e.g. user clicked through on github.com/mobile without commenting).

    `force=True` bypasses If-Modified-Since so the call always returns the
    current unread set. The conditional header otherwise short-circuits a
    manual refresh whenever the unread feed's Last-Modified matches our
    bookmark — even when local state is desynced — because GitHub's
    Last-Modified reflects the most recent unread item, not whether local
    truth matches.

    Auto-poll caps the response at ~100 items (PER_PAGE * MAX_PAGES_PER_FETCH);
    a forced fetch goes deeper, capped at ~1000 (PER_PAGE * MAX_PAGES_FORCED),
    matching expectations: auto-refresh stays cheap, manual refresh / app
    launch is thorough. When the response wasn't truncated, the "missing
    from response → mark read" sweep flips every locally-unread row that
    isn't in the response. When it was (user has more unreads than the
    cap), the sweep restricts itself to the window we actually covered
    (updated_at >= the oldest seen item) so we never wipe long-tail unreads
    we simply didn't fetch. Deeper reconciliation is a Backfill case."""
    last_modified = (
        db.get_meta(conn, "last_modified_unread")
        or db.get_meta(conn, "last_modified")  # fallback to legacy single-key
    )
    items, new_last_modified, status, truncated = _get_paginated(
        token, {"per_page": PER_PAGE}, last_modified,
        max_pages=MAX_PAGES_FORCED if force else MAX_PAGES_PER_FETCH,
        use_cache=not force,
    )
    if status == 304:
        return 0

    now = int(time.time())
    seen_ids: set[str] = set()
    oldest_seen: str | None = None
    for item in items:
        if not item.get("id"):
            continue
        seen_ids.add(item["id"])
        u = item.get("updated_at") or ""
        if u and (oldest_seen is None or u < oldest_seen):
            oldest_seen = u
        _upsert(conn, item, now, touched)

    # Items previously unread but missing from the response were read elsewhere.
    # Skip rows the user has explicitly kept-unread locally. When the response
    # was truncated (we hit the page cap and github had more), restrict the
    # sweep to the time window we actually covered so the long tail we didn't
    # fetch isn't silently wiped to read.
    placeholders = ",".join(["?"] * len(seen_ids)) if seen_ids else ""
    where = ["unread = 1", "COALESCE(action, '') != 'kept_unread'"]
    args: list[Any] = []
    if seen_ids:
        where.append(f"id NOT IN ({placeholders})")
        args.extend(seen_ids)
    if truncated and oldest_seen:
        where.append("updated_at >= ?")
        args.append(oldest_seen)
    conn.execute(
        f"UPDATE notifications SET unread = 0 WHERE {' AND '.join(where)}",
        tuple(args),
    )

    if new_last_modified:
        db.set_meta(conn, "last_modified_unread", new_last_modified)
        if db.get_meta(conn, "last_modified"):
            db.set_meta(conn, "last_modified", None)  # cleanup legacy key
    return len(items)


def _fetch_combined(
    conn: sqlite3.Connection, token: str, touched: list | None = None,
    force: bool = False,
) -> int:
    """Fetch up to 100 recent notifications (read or unread). Each item
    carries its own `unread` flag, so _upsert reconciles local read-state
    for everything the response contains. Always bounded to one page (100
    items); deeper history goes through Backfill.

    Three modes:

    - **Forced** (manual refresh, app launch) → conditional with
      `If-Modified-Since` against `last_modified_combined`. The companion
      forced unread fetch handles read-state reconciliation independently,
      so combined's only job is to surface new items / activity bumps. A
      cheap 304 when nothing's changed since the last successful poll.

    - **Auto-poll with unreads** → `?since=<earliest_unread.updated_at>`,
      no conditional. Filters the response to items whose activity could
      intersect a locally-unread row; the feed's Last-Modified tracks
      activity not `last_read_at`, so a 304 would hide silent reads
      (clicked through on github / mobile with no comment) on rows whose
      activity hasn't moved. With many unreads spanning a long window the
      response is still page-capped to 100 — `_has_unread_outside_window`
      routes the tail to the dedicated unread fetch.

    - **Auto-poll with zero unreads** → conditional same as forced.
      In-window reconciliation has no work to do, so the 304 is fine.

    Uncaught case in the non-forced paths: user marks-unread on github.com
    without new activity. Rare; recoverable via manual refresh."""
    params: dict[str, str | int] = {"per_page": 100, "all": "true"}
    last_modified = None
    if force:
        last_modified = (
            db.get_meta(conn, "last_modified_combined")
            or db.get_meta(conn, "last_modified_all")  # legacy single-key fallback
        )
    else:
        earliest_unread = conn.execute(
            "SELECT MIN(NULLIF(updated_at, '')) AS u FROM notifications "
            "WHERE unread = 1 AND COALESCE(action, '') != 'kept_unread'"
        ).fetchone()["u"]
        if earliest_unread:
            params["since"] = earliest_unread
        else:
            last_modified = (
                db.get_meta(conn, "last_modified_combined")
                or db.get_meta(conn, "last_modified_all")
            )
    items, new_last_modified, status, _ = _get_paginated(
        token, params, last_modified, max_pages=1
    )
    if status == 304:
        return 0

    now = int(time.time())
    for item in items:
        if not item.get("id"):
            continue
        _upsert(conn, item, now, touched)
    if new_last_modified:
        db.set_meta(conn, "last_modified_combined", new_last_modified)
        if db.get_meta(conn, "last_modified_all"):
            db.set_meta(conn, "last_modified_all", None)  # cleanup legacy key
        if db.get_meta(conn, "last_full_fetch_at"):
            db.set_meta(conn, "last_full_fetch_at", None)  # no longer used
    return len(items)


def _has_unread_outside_window(conn: sqlite3.Connection) -> bool:
    """True iff at least one locally-unread non-kept-unread row sits outside
    the 100 most-recently-updated rows. When False, the combined fetch's
    response covers every unread item we'd want to reconcile, so the
    dedicated unread fetch can be skipped."""
    row = conn.execute(
        """
        SELECT 1 FROM notifications
        WHERE unread = 1
          AND COALESCE(action, '') != 'kept_unread'
          AND id NOT IN (
            SELECT id FROM notifications ORDER BY updated_at DESC LIMIT 100
          )
        LIMIT 1
        """
    ).fetchone()
    return row is not None


def poll_once(
    conn: sqlite3.Connection, token: str, force_full: bool = False
) -> int:
    """One refresh cycle: combined fetch + (conditional) unread reconciliation
    + enrichment.

    The combined fetch filters its response to `since=<earliest local
    unread.updated_at>` whenever local has anything unread, so in-window
    silent reads (read on github.com / mobile, no comment) flip locally on
    the very next poll. When local has nothing unread it falls back to a
    cheap If-Modified-Since 304 — see _fetch_combined.

    The dedicated unread fetch fires only when force_full is True (manual
    refresh, app launch) or when at least one locally-unread row sits
    outside the latest-100 window — the one case the combined fetch can't
    handle, since `?all=true` doesn't surface read-on-mobile-no-comment
    events for items beyond the recent window.
    """
    touched: list = []
    n_combined = _fetch_combined(conn, token, touched, force=force_full)
    n_unread = 0
    if force_full or _has_unread_outside_window(conn):
        n_unread = _fetch_unread(conn, token, touched, force=force_full)
    new_kinds = _enrich(conn, token)
    _apply_mute_filter(conn, token, touched, new_kinds)
    _apply_throttle(conn, touched, new_kinds)
    _release_throttled(conn)
    return n_combined + n_unread
