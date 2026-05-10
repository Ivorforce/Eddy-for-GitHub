"""GitHub /notifications fetcher + upsert into SQLite."""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any

import requests

from . import db

API_NOTIFICATIONS = "https://api.github.com/notifications"
PER_PAGE = 50
MAX_PAGES_PER_FETCH = 2  # ~100 items per fetch — bound poll cost
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


def mark_read(token: str, thread_id: str) -> None:
    """Mark a notification thread as read. Stays in the inbox."""
    r = _session.patch(_thread_url(thread_id), headers=_auth_headers(token), timeout=10)
    if r.status_code in (200, 205, 304):
        return
    r.raise_for_status()


def mark_done(token: str, thread_id: str) -> None:
    """Mark as done — clears the notification from the inbox."""
    r = _session.delete(_thread_url(thread_id), headers=_auth_headers(token), timeout=10)
    if r.status_code in (204, 404):
        return  # 404 if already gone — idempotent
    r.raise_for_status()


def set_ignored(token: str, thread_id: str) -> None:
    """Set thread subscription to ignored — stops future notifications on this thread."""
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

_DISCUSSION_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    discussion(number: $number) {
      number
      title
      body
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
          bodyText
          createdAt
          lastEditedAt
          replies(first: 100) { nodes { author { login } } }
        }
      }
      reactionGroups { content reactors { totalCount } }
    }
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
    for c in comments.get("nodes") or []:
        login = (c.get("author") or {}).get("login")
        if login:
            logins.add(login)
        for rep in ((c.get("replies") or {}).get("nodes")) or []:
            rl = (rep.get("author") or {}).get("login")
            if rl:
                logins.add(rl)
        # Top-level comments only — replies still flow through commenter
        # counting above, but their bodies aren't in the AI context yet.
        comment_history.append({
            "database_id": c.get("databaseId"),
            "user": {"login": login},
            "author_association": c.get("authorAssociation"),
            "created_at": c.get("createdAt"),
            "edited_at": c.get("lastEditedAt"),
            "body": c.get("bodyText") or "",
        })

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
      url
      createdAt
      updatedAt
      state
      isDraft
      merged
      mergeStateStatus
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
      reactionGroups { content reactors { totalCount } }
      comments(last: 100) {
        totalCount
        nodes {
          databaseId
          author { login }
          authorAssociation
          bodyText
          createdAt
          lastEditedAt
        }
      }
      reviews(last: 100) {
        nodes {
          databaseId
          state
          author { login }
          authorAssociation
          bodyText
          submittedAt
          lastEditedAt
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
            "body": c.get("bodyText") or "",
        })

    # Reviews — feed _compute_review_state in REST shape, count distinct
    # non-PENDING authors. Body and submittedAt ride along so the AI
    # summary can surface change-request rationales.
    rest_reviews: list[dict] = []
    reviewer_logins: set[str] = set()
    for rev in (pr.get("reviews") or {}).get("nodes") or []:
        state = rev.get("state")
        author_login = (rev.get("author") or {}).get("login")
        rest_reviews.append({
            "database_id": rev.get("databaseId"),
            "state": state,
            "user": {"login": author_login},
            "author_association": rev.get("authorAssociation"),
            "submitted_at": rev.get("submittedAt"),
            "edited_at": rev.get("lastEditedAt"),
            "body": rev.get("bodyText") or "",
        })
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
        "state": state,
        "draft": pr.get("isDraft"),
        "merged": pr.get("merged"),
        "mergeable_state": merge_status,
        "additions": pr.get("additions"),
        "deletions": pr.get("deletions"),
        "changed_files": pr.get("changedFiles"),
        "files": files,
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
          bodyText
          createdAt
          lastEditedAt
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
            "body": c.get("bodyText") or "",
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


def _enrich(conn: sqlite3.Connection, token: str) -> None:
    """Fetch full details for up to ENRICHMENT_PER_POLL notifications that need it.

    PR / Issue / Discussion go through GraphQL (one round trip each, with
    reactions / review state / commenter counts folded in). Release goes
    through REST since the GraphQL release lookup needs the tag name.
    """
    rows = conn.execute(
        """
        SELECT id, api_url, type FROM notifications
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

        # Fan comments + reviews out into thread_events for the stateful
        # AI integration. Idempotent on the GitHub databaseId — re-fetch
        # updates the payload (catches body edits) instead of appending
        # duplicates. Comments without a databaseId (rare; only when the
        # author's account is deleted) get skipped — no stable dedup key.
        for c in comment_history:
            db_id = c.get("database_id")
            if db_id is None:
                continue
            ts = db.iso_to_unix(c.get("created_at")) or now
            db.write_thread_event(
                conn,
                thread_id=row["id"],
                ts=ts,
                kind="comment",
                source="github",
                external_id=str(db_id),
                payload={
                    "author": (c.get("user") or {}).get("login"),
                    "author_association": c.get("author_association"),
                    "body": c.get("body") or "",
                    "created_at": c.get("created_at"),
                    "edited_at": c.get("edited_at"),
                },
            )
        for rev in pr_reviews:
            db_id = rev.get("database_id")
            if db_id is None:
                continue
            ts = db.iso_to_unix(rev.get("submitted_at")) or now
            db.write_thread_event(
                conn,
                thread_id=row["id"],
                ts=ts,
                kind="review",
                source="github",
                external_id=str(db_id),
                payload={
                    "author": (rev.get("user") or {}).get("login"),
                    "author_association": rev.get("author_association"),
                    "body": rev.get("body") or "",
                    "state": rev.get("state"),
                    "submitted_at": rev.get("submitted_at"),
                    "edited_at": rev.get("edited_at"),
                },
            )
        # State transitions (merged / closed / reopened / draft↔ready).
        # Deduped on the GraphQL global node id; re-fetch leaves them
        # untouched (the events are immutable on GitHub's side once
        # they happen).
        for ev in lifecycle_events:
            ev_id = ev.get("id")
            if ev_id is None:
                continue
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

        if row["type"] == "PullRequest":
            # All four bonus signals come from the same GraphQL call — write
            # them in one statement. COALESCE on baseline_review_state keeps
            # the first-seen review state pinned so the 'pill-new' dot only
            # fires when the state actually shifts after that.
            conn.execute(
                "UPDATE notifications SET "
                "pr_reactions_json = ?, "
                "unique_commenters = ?, unique_reviewers = ?, "
                "pr_review_state = ?, "
                "baseline_review_state = COALESCE(baseline_review_state, ?) "
                "WHERE id = ?",
                (
                    json.dumps(pr_reactions) if pr_reactions is not None else None,
                    n_commenters,
                    n_reviewers,
                    review_state,
                    review_state,
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


def set_subscribed(token: str, thread_id: str) -> None:
    """Re-subscribe to a thread (reverse of set_ignored)."""
    r = _session.put(
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
        "SELECT unread FROM notifications WHERE id = ?", (item["id"],)
    ).fetchone()
    new_unread = 1 if item.get("unread") else 0
    external_read = (prev is not None and prev["unread"] == 1 and new_unread == 0)
    conn.execute(
        """
        INSERT INTO notifications (
            id, repo, type, title, reason, api_url, html_url,
            updated_at, last_read_at, unread, raw_json, fetched_at, link_url
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            link_url = COALESCE(excluded.link_url, notifications.link_url)
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
            new_unread,
            json.dumps(item),
            now,
            link_candidate,
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
    use_cache: bool = True,
) -> tuple[list[dict[str, Any]], str | None, int]:
    """GET /notifications with optional If-Modified-Since + page cap.
    Returns (items, new_last_modified, status). status=304 means no changes.
    Pass use_cache=False to skip the conditional header — needed when the
    caller wants a guaranteed re-pull even if the feed's Last-Modified
    matches the bookmark (manual refresh repairing a local desync)."""
    headers = _auth_headers(token)
    if last_modified and use_cache:
        headers["If-Modified-Since"] = last_modified
    r = _session.get(API_NOTIFICATIONS, headers=headers, params=params, timeout=30)
    if r.status_code == 304:
        return [], last_modified, 304
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
    return items, new_last_modified, 200


def _fetch_unread(
    conn: sqlite3.Connection, token: str, force: bool = False
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
    truth matches."""
    last_modified = (
        db.get_meta(conn, "last_modified_unread")
        or db.get_meta(conn, "last_modified")  # fallback to legacy single-key
    )
    items, new_last_modified, status = _get_paginated(
        token, {"per_page": PER_PAGE}, last_modified, use_cache=not force
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


def _fetch_combined(conn: sqlite3.Connection, token: str) -> int:
    """Fetch the 100 most-recently-updated notifications (read or unread) in
    a single GET. Each item carries its own `unread` flag, so _upsert
    reconciles local read-state for everything in this window. If-Modified-
    Since gives the quiet-poll path a cheap 304.

    This replaces the previous unread + since-bookmark pair for the auto-
    refresh path. The dedicated `/notifications` (unread-only) call still
    exists for the rarer reconciliation case where a locally-unread item
    sits outside the latest-100 window — see _has_unread_outside_window."""
    last_modified = (
        db.get_meta(conn, "last_modified_combined")
        or db.get_meta(conn, "last_modified_all")  # legacy single-key fallback
    )
    items, new_last_modified, status = _get_paginated(
        token, {"per_page": 100, "all": "true"}, last_modified, max_pages=1
    )
    if status == 304:
        return 0

    now = int(time.time())
    for item in items:
        if not item.get("id"):
            continue
        _upsert(conn, item, now)
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

    The combined fetch covers virtually all reconciliation in normal use.
    The dedicated unread fetch fires only when force_full is True (manual
    refresh, app launch) or when at least one locally-unread row sits
    outside the latest-100 window — that's the only case the combined fetch
    can't handle, since `?all=true` doesn't surface read-on-mobile-no-comment
    events for items beyond the recent window.
    """
    n_combined = _fetch_combined(conn, token)
    n_unread = 0
    if force_full or _has_unread_outside_window(conn):
        n_unread = _fetch_unread(conn, token, force=force_full)
    _enrich(conn, token)
    return n_combined + n_unread
