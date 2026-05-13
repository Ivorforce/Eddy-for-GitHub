"""GitHub /notifications fetcher + upsert into SQLite."""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from typing import Any

import requests

from . import db

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
# The id lands verbatim in DOM ids / CSS selectors, so it stays "_"-joined
# (a ":" is a CSS syntax error); real thread ids are all-digits, so "q_" is
# unambiguous. node_ids are [A-Za-z0-9_-], i.e. valid CSS identifier tails.
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
            "id": "q_" + node_id,
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
        "id": "q_" + node_id,
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
    if payload.get("errors"):
        log.warning(
            "GraphQL errors fetching PR %s/%s#%s: %s",
            owner, name, number, payload["errors"],
        )
        return None
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
    requested_reviewers: list[dict] = []
    requested_teams: list[dict] = []
    for rr in (pr.get("reviewRequests") or {}).get("nodes") or []:
        rev = rr.get("requestedReviewer") or {}
        if rev.get("__typename") == "User" and rev.get("login"):
            requested_reviewers.append({"login": rev["login"]})
        elif rev.get("__typename") == "Team" and rev.get("slug"):
            requested_teams.append({"slug": rev["slug"]})

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
    if payload.get("errors"):
        log.warning(
            "GraphQL errors fetching issue %s/%s#%s: %s",
            owner, name, number, payload["errors"],
        )
        return None
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
        "_unique_commenters": len(commenter_logins),
    }


def fetch_release(token: str, api_url: str | None) -> dict | None:
    """REST fetch for a Release. Returns a payload shaped to slot into the
    same details_json path as Issues (html_url / created_at / user /
    reactions) so popularity + age pills read it without branching.

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
    }


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
        # without counting it as new — same shape as baseline_comments.
        new_oid = None
        if row["type"] == "PullRequest":
            new_oid = (details.get("last_commit") or {}).get("abbrev_oid")
            if new_oid and row["baseline_head_oid"] and new_oid != row["baseline_head_oid"]:
                nk.add("code")
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
            continue

        # Issues + Discussions: only bonus key is the commenter count.
        if n_commenters is not None:
            conn.execute(
                "UPDATE notifications SET unique_commenters = ? WHERE id = ?",
                (n_commenters, row["id"]),
            )

    return new_kinds


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
        "snooze_until, updated_at FROM notifications WHERE id = ?",
        (item["id"],),
    ).fetchone()
    if touched is not None and prev is not None:
        touched.append((
            item["id"], prev["unread"], prev["action"], prev["ignored"],
            prev["effective_updated_at"], prev["snooze_until"],
        ))
    new_unread = 1 if item.get("unread") else 0
    external_read = (prev is not None and prev["unread"] == 1 and new_unread == 0)
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
    if reason:
        _accumulate_seen_reason(conn, item["id"], reason)
    if external_read:
        db.write_thread_event(
            conn,
            thread_id=item["id"],
            ts=now,
            kind="user_action",
            source="github",
            payload={"action": "read_on_github"},
        )
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
    """Whether the bystander-throttle toggle is on. Default on; only the
    explicit string 'off' disables it."""
    return (db.get_meta(conn, "quiet_bystanders") or "on").strip().lower() != "off"


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
