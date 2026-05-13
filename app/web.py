"""Flask app + routes."""
from __future__ import annotations

import json
import logging
import math
import time
from datetime import datetime, timezone

from flask import Flask, Response, make_response, render_template, request
from markupsafe import Markup

from . import ai, db, events, ghmd, github, settings

log = logging.getLogger(__name__)

app = Flask(
    __name__,
    template_folder="../templates",
    static_folder="../static",
)


def _humanize(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    secs = int((datetime.now(timezone.utc) - dt).total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    days = secs / 86400.0
    # Same w/mo/y vocabulary as the age pill (see _age_pill) so the "Updated
    # … ago" line in its tooltip reads in the same units as the pill text.
    if days < 7:
        return f"{int(days)}d ago"
    if days < 30:
        return f"{int(days / 7)}w ago"
    if days < 365:
        return f"{int(days / 30.44)}mo ago"
    return f"{int(days / 365.25)}y ago"


app.jinja_env.filters["humanize"] = _humanize


# Item-creation age (NOT notification age — that's already encoded by row order
# and exposed on hover). Surfaced as a colored pill so a year-old PR that just
# came back to life is instantly distinguishable from a fresh one.
#
# Color uses log(days+1) → [0,1] over 0..AGE_GRADIENT_MAX_DAYS. Log compresses
# the long tail (year-vs-five-year matters less than week-vs-month) and gives
# the dense first-month range most of the visual range. Hue interpolates
# muted-green → faded-warm-orange.
AGE_GRADIENT_MAX_DAYS = 1825  # 5y; older items pin to the warm end


def _age_pill(iso: str | None, subject_type: str | None = None) -> dict | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    secs = max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
    days = secs / 86400.0

    if secs < 3600:
        text = "now"
    elif secs < 86400:
        text = f"{secs // 3600}h"
    elif days < 7:
        text = f"{int(days)}d"
    elif days < 30:
        text = f"{int(days / 7)}w"
    elif days < 365:
        text = f"{int(days / 30.44)}mo"
    else:
        text = f"{int(days / 365.25)}y"

    t = min(1.0, math.log(days + 1) / math.log(AGE_GRADIENT_MAX_DAYS + 1))
    # HSL endpoints picked to read in both light and dark mode: moderate
    # saturation, mid lightness. Green→warm-orange goes through yellow-green,
    # which keeps the "fresh" hue from blurring into the "old" hue.
    hue = 140 + (25 - 140) * t
    sat = 35
    lit = 45 + (50 - 45) * t
    color = f"hsl({hue:.0f} {sat:.0f}% {lit:.0f}%)"

    days_int = int(days)
    verb = "Published" if subject_type == "Release" else "Created"
    title = f"{verb} {days_int} day{'' if days_int == 1 else 's'} ago ({iso})"
    return {"text": text, "color": color, "title": title}


# Popularity colors split sentiment into two independent visual axes:
#   hue        ← ratio (pure positive → green, balanced → yellow, pure negative → red)
#   saturation ← total volume (1 vote barely tints, 20+ reads vivid)
# So 4+/2- (positive but contested) reads as moderate yellow-green, while
# 30+/0- reads as strong pure green. The displayed number is still net.
POPULARITY_GRADIENT_MAX = 50


def _popularity_pill(reception: dict | None) -> dict | None:
    if not reception:
        return None
    pos = reception.get("pos") or 0
    neg = reception.get("neg") or 0
    if not (pos or neg):
        return None
    net = pos - neg
    mag = abs(net)
    total = pos + neg
    arrow = "▲" if net > 0 else ("▼" if net < 0 else "")
    # Hue: linear from green (120) at ratio=1 to red (0) at ratio=0.
    # Lightness: pull yellow/red end up so they don't read muddy.
    ratio = pos / total
    hue = 120 * ratio
    lit = 36 + 12 * (1 - ratio)
    # Saturation curve: log scale on total volume, then ^1.5 to suppress the
    # low end so a single vote reads as barely-tinted gray rather than a
    # confident signal. Floor at 8% so colored hue is just visible.
    t = (math.log(total + 1) / math.log(POPULARITY_GRADIENT_MAX + 1)) ** 1.5
    sat = 8 + 62 * t
    color = f"hsl({hue:.0f} {sat:.0f}% {lit:.0f}%)"
    title = f"{pos} positive · {neg} negative ({total} reaction{'' if total == 1 else 's'})"
    return {"mag": str(mag), "arrow": arrow, "color": color, "title": title}


_ROW_COLS = (
    "id, repo, type, title, reason, html_url, link_url, updated_at, "
    "effective_updated_at, "
    "unread, ignored, action, action_source, "
    "details_json, details_fetched_at, "
    "seen_reasons, baseline_comments, muted_kinds, "
    "pr_reactions_json, unique_commenters, unique_reviewers, "
    "pr_review_state, baseline_review_state, "
    "note_user, is_tracked, priority_user, snooze_until, "
    "ai_verdict_json, ai_verdict_at, ai_verdict_model"
)

# Per-thread notification-kind filter. The taxonomy (MUTE_KINDS, which type
# produces which) lives in github.py — _enrich is what emits the events; here
# we just add the display labels. Labels are spelled out (no tooltips — a
# tooltip inside the [popover] menu renders behind it). `lifecycle` isn't
# mutable but still gets a label: the menu shows it as a disabled row so it
# doesn't look like a missing toggle.
MUTE_KINDS = github.MUTE_KINDS
_MUTE_KINDS_BY_TYPE = github.MUTE_KINDS_BY_TYPE
MUTE_KIND_UI = (
    ("comment",   "Comments"),
    ("review",    "Reviews"),
    ("code",      "Code pushes"),
    ("lifecycle", "State changes"),
)
_MUTE_KIND_LABEL = dict(MUTE_KIND_UI)


def _mute_kind_options(notif_type: str) -> list[tuple[str, str, bool]]:
    """(key, label, mutable) rows for the ▾ menu of a notification of this type:
    the mutable kinds as toggles, plus a non-mutable `lifecycle` row (greyed —
    state changes always notify) on types that produce lifecycle events. Empty
    for types with no filter UI at all (Release, CheckSuite, …)."""
    mutable = _MUTE_KINDS_BY_TYPE.get(notif_type, ())
    if not mutable:
        return []
    opts: list[tuple[str, str, bool]] = [
        (k, lbl, True) for k, lbl in MUTE_KIND_UI if k in mutable
    ]
    opts.append(("lifecycle", _MUTE_KIND_LABEL["lifecycle"], False))
    return opts


# Priority is a 0..1 float end-to-end — the AI emits one (`priority_score`),
# the user's hand-pin is stored as one (`notifications.priority_user`), and
# `priority_change` timeline events carry floats. The six named bands below
# are a *display* layer over that float: a shared vocabulary for the pill
# tint, the picker, tooltips and timeline text, whose boundaries can be
# re-tuned here (and in ai_system_prompt.md §Priority) without migrating any
# stored data. _BANDS = exclusive-upper thresholds, low → high; _LEVEL_SCORE
# = the representative float written when the user picks a band; _DESC = the
# tooltip / anchor gloss; PRIORITY_GROUPS = the 2-2-2 visual grouping
# (Low / Medium / High, each label spanning a pair of sub-levels) the picker
# renders as a single row.
_PRIORITY_BANDS = (
    (0.10, "irrelevant"),
    (0.30, "minor"),
    (0.50, "routine"),
    (0.65, "normal"),
    (0.85, "high"),
    (1.01, "urgent"),
)
PRIORITY_LEVELS = tuple(name for _, name in _PRIORITY_BANDS)
# Where unassessed rows sort in priority order: just under the routine band, so
# they beat irrelevant + minor (the user has signalled "don't bother") but lose
# to anything explicitly pinned routine-or-above. Stored nowhere — sort key only.
_UNASSESSED_SORT_SCORE = 0.29
_PRIORITY_LEVEL_SCORE = {
    "irrelevant": 0.05, "minor": 0.20, "routine": 0.40,
    "normal":     0.57, "high":  0.75, "urgent":  0.93,
}
_PRIORITY_LEVEL_DESC = {
    "irrelevant": "Irrelevant",
    "minor":      "Mostly irrelevant",
    "routine":    "low priority",
    "normal":     "normal priority",
    "high":       "high priority",
    "urgent":     "urgent",
}
PRIORITY_GROUPS = (
    ("Low",    ("irrelevant", "minor")),
    ("Medium", ("routine", "normal")),
    ("High",   ("high", "urgent")),
)
# Flattened for the template: ({group, cells: ({key, desc, score}, ...)}, ...).
PRIORITY_UI = tuple(
    {
        "group": label,
        "cells": tuple(
            {"key": k, "desc": _PRIORITY_LEVEL_DESC[k], "score": _PRIORITY_LEVEL_SCORE[k]}
            for k in members
        ),
    }
    for label, members in PRIORITY_GROUPS
)
app.jinja_env.globals["PRIORITY_UI"] = PRIORITY_UI

def _clamp01(x) -> float | None:
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return None


def _score_to_priority_level(score: float) -> str:
    """Bucket a 0..1 priority score into one of the named bands."""
    s = _clamp01(score) or 0.0
    for hi, name in _PRIORITY_BANDS:
        if s < hi:
            return name
    return _PRIORITY_BANDS[-1][1]


def _verdict_priority(verdict: dict) -> tuple[str, float]:
    """(band name, score) for a verdict payload. The AI emits `priority_score`
    (a float); falls back to 'normal'/0.5 if it's missing or unparseable."""
    score = _clamp01(verdict.get("priority_score"))
    if score is None:
        score = 0.5
    return _score_to_priority_level(score), score


# author_association -> (badge css class, display label).
# Only high-signal associations get a badge; CONTRIBUTOR/NONE etc. stay quiet.
_AUTHOR_BADGE = {
    "OWNER":                  ("member",     "owner"),
    "MEMBER":                 ("member",     "member"),
    "COLLABORATOR":           ("collab",     "collab"),
    "FIRST_TIMER":            ("first-time", "first-time"),
    "FIRST_TIME_CONTRIBUTOR": ("first-time", "first-time"),
}


def _author_badge_class(login: str | None, assoc: str | None, user_login: str | None) -> str:
    """Pick the badge CSS class for an author rendering. The user's own
    login wins over any association ('self' icon); otherwise the
    _AUTHOR_BADGE lookup applies. Empty string when there's no badge —
    template renders the muted generic person icon.

    Centralized here so both the existing Repo column (where the row
    author is the lookup target) and the popover timeline (where each
    commenter / reviewer is) derive the badge from one place."""
    if user_login and login == user_login:
        return "self"
    badge = _AUTHOR_BADGE.get(assoc or "")
    return badge[0] if badge else ""

# GitHub reaction emoji buckets. Same user can react with multiple positives;
# max() approximates a lower bound on distinct users in that sentiment bucket
# (sum overcounts; max never overcounts a single category).
# 'upvotes' is a Discussion-only synthetic key: discussion enrichment folds
# upvoteCount into the reactions dict so it participates in the positive max
# alongside the emoji buckets. Other types simply lack the key.
_POSITIVE_REACTIONS = ("+1", "heart", "hooray", "rocket", "laugh", "upvotes")
_NEGATIVE_REACTIONS = ("-1", "confused")
_INTEREST_REACTION = "eyes"

TYPE_LABELS = {
    "PullRequest": "PR",
    "Issue": "ISS",
    "Discussion": "Disc",
    "Release": "Rel",
    "CheckSuite": "CHK",
}

# Long form for places that have room (e.g. popover headers).
TYPE_LABELS_LONG = {
    "PullRequest": "Pull Request",
    "Issue": "Issue",
    "Discussion": "Discussion",
    "Release": "Release",
    "CheckSuite": "Check Suite",
}

# Each row carries up to three independent indicators:
#   action_needed: 'assigned' | 'review_you' | 'review_team' | None  (mutually exclusive)
#   mentioned_since: bool      (since last action)
#   is_author: bool
#   is_involved: bool          (author / directed reason / active-participant reason)


def _aggregate_reactions(reactions: dict | None) -> tuple[int, int, int]:
    """Return (positive_max, negative_max, eyes). See _POSITIVE_REACTIONS docstring."""
    if not reactions:
        return (0, 0, 0)
    pos = max((reactions.get(k) or 0) for k in _POSITIVE_REACTIONS)
    neg = max((reactions.get(k) or 0) for k in _NEGATIVE_REACTIONS)
    eyes = reactions.get(_INTEREST_REACTION) or 0
    return (pos, neg, eyes)


# mergeable_state -> (display label, severity).
# 'blocked' is dropped: it's near-synonymous with "needs review", which we
# already convey via the Review you/team pills.
_MERGE_STATE_DISPLAY = {
    "dirty":    ("conflicts",   "danger"),
    "unstable": ("CI failing",  "warning"),
    "behind":   ("behind base", "warning"),
}

# type_state values that mean "this thread is resolved" — drives the
# Hide resolved filter. Distinct from "closed" in any single GitHub sense:
# PRs report 'merged' or 'closed_pr', Issues report 'closed_completed'
# or 'closed_not_planned', Discussions report 'answered' or 'closed'.
# All count as resolved for triage purposes — nothing left to do.
RESOLVED_TYPE_STATES = {
    "merged",
    "closed_pr",
    "closed_completed",
    "closed_not_planned",
    "answered",
    "closed",
}


def _merge_state(details: dict, subject_type: str) -> tuple[str, str] | None:
    """Mergeable-state warning for the Status column. (label, severity) or None."""
    if subject_type != "PullRequest":
        return None
    state = details.get("mergeable_state")
    return _MERGE_STATE_DISPLAY.get(state)


def _format_meta(
    details_json: str | None,
    subject_type: str,
    baseline_comments: int | None,
    pr_reactions_json: str | None = None,
    unique_commenters: int | None = None,
    unique_reviewers: int | None = None,
) -> dict:
    """Title sub-line metrics, split along three independent axes:
        complexity: PR diff size (additions, deletions)
        reception:  sentiment polarity (positive max, negative max)
        interest:   attention volume (comments + new, distinct-reacter approximation)
        top_files:  up to 5 most-changed files for diff-label hover tooltip
    Each is None when not applicable so the pill hides."""
    out = {"complexity": None, "reception": None, "interest": None, "top_files": None}
    if not details_json:
        return out
    try:
        d = json.loads(details_json)
    except (ValueError, TypeError):
        return out

    if subject_type == "PullRequest":
        adds = d.get("additions") or 0
        dels = d.get("deletions") or 0
        if adds or dels:
            out["complexity"] = (adds, dels)
        # Top 5 files by total changed lines, for the diff-label hover. Sort
        # is descending on additions+deletions so the user sees the biggest
        # touch points first; ties break on filename for stability across
        # re-renders.
        files = d.get("files") or []
        ranked = sorted(
            (f for f in files if f.get("filename")),
            key=lambda f: (
                -((f.get("additions") or 0) + (f.get("deletions") or 0)),
                f.get("filename") or "",
            ),
        )[:5]
        if ranked:
            out["top_files"] = ranked

    # Reactions: PRs come from the separately-fetched issue-form endpoint;
    # Issues already have them embedded in details_json.
    rx_dict: dict | None = None
    if subject_type == "PullRequest" and pr_reactions_json:
        try:
            rx_dict = json.loads(pr_reactions_json)
        except (ValueError, TypeError):
            rx_dict = None
    elif subject_type in ("Issue", "Discussion", "Release"):
        rx_dict = d.get("reactions")
    pos, neg, eyes = _aggregate_reactions(rx_dict)

    # Reception: only sentiment polarity (votes), no eyes.
    if pos or neg:
        out["reception"] = {"pos": pos, "neg": neg}

    # Interest: comments + distinct-reacter approximation across all categories.
    comments = d.get("comments") or 0
    new_comments = (
        comments - baseline_comments
        if comments > 0 and baseline_comments is not None and comments > baseline_comments
        else None
    )
    # Engagement = max-aggregated reactions + unique commenter count + unique
    # reviewer count. Reviewers and commenters can overlap (and reactions can
    # overlap with both); engaged is an approximation, matching the existing
    # double-counting between reactions and commenters.
    engaged = pos + neg + eyes + (unique_commenters or 0) + (unique_reviewers or 0)
    if comments > 0 or engaged > 0:
        out["interest"] = {
            "comments": comments,
            "new_comments": new_comments,
            "engaged": engaged,
            "commenters": unique_commenters,
            "reviewers": unique_reviewers,
        }

    return out


def _label_text_color(hex_color: str | None) -> str:
    """Return '#fff' or '#000' for best contrast on the given GitHub label background."""
    if not hex_color or len(hex_color) != 6:
        return "#000"
    try:
        r, g, b = (int(hex_color[i : i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return "#000"
    luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    return "#fff" if luminance < 0.55 else "#000"


def _extract_labels(details_json: str | None) -> list[dict]:
    """Pull GitHub label dicts (name, color, description) from cached details."""
    if not details_json:
        return []
    try:
        d = json.loads(details_json)
    except (ValueError, TypeError):
        return []
    out = []
    for l in d.get("labels") or []:
        out.append(
            {
                "name": l.get("name") or "",
                "color": l.get("color") or "888888",
                "description": l.get("description") or "",
                "text_color": _label_text_color(l.get("color")),
            }
        )
    return out


def _bucket(date_iso: str | None) -> str:
    """Group items into time buckets based on local-calendar age. Caller
    decides which date field to bucket by — the sort key drives that
    choice (updated_at for Most recent / Most stale, created_at for
    Newest / Oldest)."""
    if not date_iso:
        return "Earlier"
    try:
        dt = datetime.fromisoformat(date_iso.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return "Earlier"
    today = datetime.now().astimezone().date()
    delta = (today - dt.date()).days
    if delta <= 0:
        return "Today"
    if delta == 1:
        return "Yesterday"
    if delta < 7:
        return "This week"
    if delta < 30:
        return "This month"
    return "Earlier"


def _split_repo(repo: str) -> tuple[str, str]:
    if "/" in repo:
        owner, name = repo.split("/", 1)
        return owner, name
    return "", repo


def _action_needed(
    details: dict, repo_owner: str, current_reason: str, seen: set[str]
) -> str | None:
    """Return one of 'assigned' | 'review_you' | 'review_team' | None.

    Prefers details_json (cached PR/Issue object) for accuracy. Falls back to
    the notification reason when details aren't available yet."""
    user_login: str | None = app.config.get("USER_LOGIN")
    user_teams: set[tuple[str, str]] = app.config.get("USER_TEAMS") or set()

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

    # No cached details — best-effort hint from reason. We don't know you-vs-team
    # for review_requested without details, so default to review_team (the more
    # common case for org maintainers); enrichment will correct on next poll.
    if "assign" in seen or current_reason == "assign":
        return "assigned"
    if "review_requested" in seen or current_reason == "review_requested":
        return "review_team"
    return None


def _is_author(details: dict) -> bool:
    user_login: str | None = app.config.get("USER_LOGIN")
    if not user_login:
        return False
    return (details.get("user") or {}).get("login") == user_login


def _mentioned_since(seen: set[str]) -> bool:
    return "mention" in seen or "team_mention" in seen


_ACTION_LABELS = {
    "assigned": "Assigned",
    "review_you": "Review you",
    "review_team": "Review team",
}
# Filter-side labels: same three plus 'mentioned' (a separate row signal,
# not a value of action_needed). Drives the "Action" filter-bar dropdown.
ACTION_FILTER_LABELS = {**_ACTION_LABELS, "mentioned": "Mentioned"}
_REVIEW_LABELS = {
    "approved": "Approved",
    "changes_requested": "Changes requested",
}
# Last-resort labels keyed off GitHub's notification `reason`. Used only when
# nothing more specific (action, mention, review state, new comments, …)
# applies, so the thread pill still shows *why* the row is here.
_REASON_FALLBACK_LABELS = {
    "subscribed": "Subscribed",
    "author": "Your thread",
    "comment": "Commented before",
    "state_change": "State changed",
    "push": "New commit",
    "ci_activity": "CI activity",
    "security_alert": "Security alert",
    "security_advisory_credit": "Advisory credit",
    "manual": "Manually subscribed",
    "invitation": "Invitation",
    "approval_requested": "Approval requested",
    "review_requested": "Review requested",
    "mention": "Mentioned",
    "team_mention": "Team mentioned",
    "assign": "Assigned",
    "member_feature_requested": "Org request",
}


def _thread_pill(d: dict) -> dict:
    """Activity / state summary for the manual-mode relevance pill: one
    headline (the most action-defining signal), an optional truncated subtext
    (the remaining signals, ' · '-joined), and an optional full-breakdown
    tooltip. All derived from baseline diffs / current state — never from the
    thread_events log — so the indicators persist across user actions.

    Priority is action-defining over merely-blocking: "Review you" tells you
    what to do; "Conflicts" only says something is broken (still listed in the
    subtext). When nothing fires, fall back to the GitHub `reason` so the row
    still shows why it's here; if even that is empty, headline is None and the
    pill renders icon-only."""
    # (rank, text)
    candidates: list[tuple[int, str]] = []

    merge = d.get("merge_state")
    if merge:
        candidates.append((3 if merge[1] == "danger" else 5, merge[0]))

    action = d.get("action_needed")
    if action:
        candidates.append((1, _ACTION_LABELS[action]))

    if d.get("mentioned_since"):
        candidates.append((2, "Mentioned"))

    rs = d.get("pr_review_state")
    if rs in _REVIEW_LABELS:
        candidates.append((4, _REVIEW_LABELS[rs]))

    interest = (d.get("meta") or {}).get("interest") or {}
    new_c = interest.get("new_comments") or 0
    if new_c:
        candidates.append((6, f"+{new_c} comment{'' if new_c == 1 else 's'}"))

    candidates.sort(key=lambda c: c[0])
    texts = [t for _, t in candidates]
    if not texts:
        fallback = _REASON_FALLBACK_LABELS.get(d.get("reason") or "")
        return {"headline": fallback or None, "subtext": None, "tip": None}
    return {
        "headline": texts[0],
        "subtext": " · ".join(texts[1:]) or None,
        "tip": " · ".join(texts) if len(texts) > 1 else None,
    }


def _type_state(details: dict, subject_type: str) -> str:
    """One of: open, draft, merged, closed_pr, closed_completed,
    closed_not_planned, or 'unknown' if we can't tell yet."""
    if not details:
        return "unknown"
    if subject_type == "PullRequest":
        if details.get("merged"):
            return "merged"
        if details.get("draft"):
            return "draft"
        state = details.get("state")
        if state == "open":
            return "open"
        if state == "closed":
            return "closed_pr"
        return "unknown"
    if subject_type == "Issue":
        state = details.get("state")
        if state == "open":
            return "open"
        if state == "closed":
            return "closed_not_planned" if details.get("state_reason") == "not_planned" else "closed_completed"
        return "unknown"
    if subject_type == "Discussion":
        # Discussion enrichment puts 'answered' / 'closed' / 'open' on
        # details.state; 'answered' is reused as a "successful outcome"
        # signal even when the discussion is later closed.
        state = details.get("state")
        if state in ("answered", "closed", "open"):
            return state
        return "unknown"
    return "unknown"


def _tracked_people() -> set[str]:
    conn = db.connect()
    try:
        return {
            r["login"] for r in conn.execute(
                "SELECT login FROM people WHERE is_tracked = 1"
            ).fetchall()
        }
    finally:
        conn.close()


def _tracked_set(table: str) -> set[str]:
    """Generic tracked-set lookup for the repos / orgs tables (both keyed
    by 'name'). Mirrors _tracked_people but kept separate because people
    use 'login' as the key."""
    conn = db.connect()
    try:
        return {
            r["name"] for r in conn.execute(
                f"SELECT name FROM {table} WHERE is_tracked = 1"
            ).fetchall()
        }
    finally:
        conn.close()


def _entity_notes(table: str, key_col: str) -> dict[str, str]:
    """All non-null notes from people / repos / orgs. Small dict (rows
    only exist for entities the user has touched: tracked or note-edited)."""
    conn = db.connect()
    try:
        return {
            r[key_col]: r["note_user"] for r in conn.execute(
                f"SELECT {key_col}, note_user FROM {table} "
                "WHERE note_user IS NOT NULL AND note_user != ''"
            ).fetchall()
        }
    finally:
        conn.close()


def _humanize_age(secs: int) -> str:
    """Compact relative-time label for the popover timeline + verdict
    metadata. Truncating to one unit keeps it inside the meta-row."""
    secs = max(0, secs)
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _short_duration(secs: int) -> str:
    """Compact 'time remaining' label (no 'ago' suffix) for the in-row
    snoozed-state button — '3d', '5h', '12m'."""
    secs = max(0, secs)
    if secs < 3600:
        return f"{max(1, secs // 60)}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


# Display labels for user_action events in the popover timeline. Source
# of truth for action strings is web._apply_action; keep this in sync if
# it grows new action labels. Unknown actions render as the raw key
# (forward-compat).
_USER_ACTION_LABELS = {
    "visited":          "Last opened link",
    "read":             "Marked read",
    "read_on_github":   "Marked read remotely",
    "muted":            "Muted",
    "done":             "Archived",
    "undone":           "Restored from archive",
    "kept_unread":      "Kept unread",
    "unmuted":          "Unmuted",
    "snoozed":          "Snoozed",
    "unsnoozed":        "Un-snoozed",
    "woken":            "Woke from snooze",
    "absorbed":         "Absorbed — muted activity",
}


# PR review state → (display label, CSS modifier class). Title-case
# labels match the popover's chat-prose tone better than the GraphQL
# enum's SHOUTED form. The class drives a colored text treatment that
# matches the state's affordance: green for approval, the existing
# desaturated red tone for change-requests, muted for dismissed/pending,
# default for plain comments.
_REVIEW_STATE = {
    "APPROVED":          ("Approved",          "state-approved"),
    "CHANGES_REQUESTED": ("Requested Changes", "state-changes-requested"),
    "DISMISSED":         ("Dismissed review",  "state-dismissed"),
    "COMMENTED":         ("Reviewed",          "state-commented"),
    "PENDING":           ("Pending",           "state-pending"),
}


# Lifecycle action → (display verb, CSS modifier class). The verb reads
# as past tense following the actor name ("alice merged"); the class
# colors the verb so a thread's chronology is scannable at a glance —
# merged purple-ish (matches GitHub's merge color), closed muted-red,
# reopened green, draft↔ready stays neutral. Issue close-reasons append
# a parenthetical to "closed" via app/web.py rendering.
_LIFECYCLE_LABEL = {
    "merged":             ("merged",                  "lifecycle-merged"),
    "closed":             ("closed",                  "lifecycle-closed"),
    "reopened":           ("reopened",                "lifecycle-reopened"),
    "ready_for_review":   ("marked ready for review", "lifecycle-neutral"),
    "converted_to_draft": ("converted to draft",      "lifecycle-neutral"),
    "answered":           ("marked answered",         "lifecycle-merged"),
}


def _verdict_render_dict(
    payload: dict, *, cur_repo: str | None = None,
    tracked_people=frozenset(), notes_people: dict | None = None,
) -> dict:
    """Display-ready bits of an ai_verdict event's payload, for rendering
    inside the timeline list. Distinct from _ai_verdict_dict, which shapes
    the *cached* verdict for the row pill (and adds stale logic that
    doesn't apply to historical entries)."""
    priority_level, priority_score = _verdict_priority(payload)

    def _md(text):
        return ghmd.render(
            text, cur_repo=cur_repo, interactive=True,
            tracked_people=tracked_people, people_notes=notes_people,
        )

    description = (payload.get("description") or "").strip()
    # `reply` is optional — present only when the AI had something to say
    # back to the user. The verdict bubble always shows `description` as a
    # recessed aside; when `reply` is present it sits above as the body.
    # See templates/_timeline_event.html.
    reply = (payload.get("reply") or "").strip()
    description_html = _md(description)
    reply_html = _md(reply) if reply else Markup("")
    model = payload.get("model") or ""
    return {
        "description":      description,
        "description_html": description_html,
        "reply":            reply,
        "reply_html":       reply_html,
        "priority_level": priority_level,
        "priority_score": priority_score,
        "model":          model,
    }


# Comments closer than this collapse into a single timeline line
# ("5 comments by alice, bob"). Any larger gap reads as a separate
# conversation worth its own entry — picked at ~1 month so a single
# discussion (typically minutes-to-days of activity) stays grouped while
# distinct flare-ups months apart don't merge into a misleading summary.
_COMMENT_COALESCE_GAP_SECS = 30 * 86400


# user_action kinds that repeat verbatim every activity round — clicking
# through to GitHub again, hitting Mark read again, archiving again. The
# latest occurrence carries the current state; earlier ones are noise. They
# each survive in their true chronological slot, just deduped to one. Not
# listed here: `muted` / `snoozed`, which are stickier states whose paired
# revert events (`unmuted` / `unsnoozed` / `woken`) tell a real story; and
# the dismissal reverts themselves, which only matter as part of an
# adjacent streak (handled by _coalesce_user_actions).
_RECURRING_ACTIONS = {"visited", "done", "read", "read_on_github"}


def _coalesce_recurring_actions(events: list[dict]) -> list[dict]:
    """For each action in _RECURRING_ACTIONS, keep only its latest occurrence
    in the whole timeline. The AI still sees every occurrence (raw
    thread_events); this only thins the visual popover. Events arrive
    chronologically, so the last occurrence per kind is the newest."""
    latest_idx: dict[str, int] = {}
    for i, ev in enumerate(events):
        if ev.get("kind") != "user_action":
            continue
        action = (ev.get("payload") or {}).get("action")
        if action in _RECURRING_ACTIONS:
            latest_idx[action] = i
    keep = set(latest_idx.values())
    def is_superseded(i: int, ev: dict) -> bool:
        if ev.get("kind") != "user_action":
            return False
        action = (ev.get("payload") or {}).get("action")
        return action in _RECURRING_ACTIONS and i not in keep
    return [ev for i, ev in enumerate(events) if not is_superseded(i, ev)]


def _coalesce_body_edits(events: list[dict]) -> list[dict]:
    """Drop earlier `body_edit` events when adjacent in the timeline (no
    other event of any kind between them). Each one says "the description
    changed"; only the most recent matters — its `at` and `editor` describe
    the body the AI / the user is now looking at, and the in-between states
    are gone (we never kept them). Unlike _coalesce_recurring_actions this
    only collapses *adjacent* runs — a body_edit is informative in its slot
    between activity, an old visit isn't."""
    out: list[dict] = []
    for ev in events:
        if (
            ev.get("kind") == "body_edit"
            and out
            and out[-1].get("kind") == "body_edit"
        ):
            out[-1] = ev   # replace older edit with newer
        else:
            out.append(ev)
    return out


def _drop_close_after_merge(events: list[dict]) -> list[dict]:
    """A merged PR emits both a `merged` and a `closed` lifecycle event —
    GitHub closes the PR as part of the merge. The `closed` line carries
    no new information then (merge always closes), so drop it when it
    immediately follows the `merged` line. Stand-alone `closed` events —
    PRs closed without merging, issues closed — are untouched."""
    out: list[dict] = []
    for ev in events:
        if (
            ev.get("kind") == "lifecycle"
            and (ev.get("payload") or {}).get("action") == "closed"
            and out
            and out[-1].get("kind") == "lifecycle"
            and (out[-1].get("payload") or {}).get("action") == "merged"
        ):
            continue
        out.append(ev)
    return out


# Dismissal-style user actions: row-state changes via the Ignore / Done / Mute
# buttons (or their reverts). Streaks of these with no other event in between
# represent user oscillation and collapse to just the latest — `visited` /
# `read_on_github` aren't included here because they're engagement / external
# observations, not the user toggling dismissal state.
_DISMISSAL_ACTIONS = {
    "read", "done", "muted", "kept_unread", "undone", "unmuted",
    "snoozed", "unsnoozed",
}
# Reverts: kept_unread reverts read, undone reverts done, unmuted reverts
# muted, unsnoozed reverts snoozed. When the latest event in a streak is the
# revert of the one immediately before it, both vanish — the streak's net
# effect is zero, no need to clutter the timeline with the vacillation.
_REVERT_OF = {
    "kept_unread": "read",
    "undone":      "done",
    "unmuted":     "muted",
    "unsnoozed":   "snoozed",
}


def _mark_superseded_reviews(events: list[dict]) -> list[dict]:
    """Flag review events that a later review by the same author overrode.
    GitHub keeps every review in the log, but only each author's most-recent
    review drives the PR's state — so an earlier 'changes requested' followed
    by an 'approved' from the same person is stale context. The template
    dims flagged events. (COMMENTED reviews are already folded into
    comment groups by _coalesce_comments, so the survivors here are the
    state-bearing ones.) Mutates events in place; returns the list."""
    last_review_idx: dict[str, int] = {}
    for idx, ev in enumerate(events):
        if ev.get("kind") == "review":
            actor = ev.get("actor") or ""
            if actor and actor != "?":
                last_review_idx[actor] = idx
    for idx, ev in enumerate(events):
        if ev.get("kind") == "review":
            actor = ev.get("actor") or ""
            if last_review_idx.get(actor, idx) > idx:
                ev["superseded"] = True
    return events


def _drop_superseded_verdicts(events: list[dict]) -> list[dict]:
    """Keep only the latest `ai_verdict` event in the rendered timeline. A
    verdict is the AI's standing take as of its timestamp; earlier ones are
    stale opinions, not facts. The DB keeps every verdict (a `skip` lands a
    fresh row carrying the prior payload, so the survivor here ends up being
    that row when nothing material has changed); the timeline pares to a
    single bubble. Attaches `earlier_verdicts_count` to the survivor so the
    bubble can show a small "↺ N" marker — an affordance signaling history
    exists. Mutates the survivor in place; returns the filtered list."""
    verdict_idxs = [i for i, ev in enumerate(events) if ev.get("kind") == "ai_verdict"]
    if len(verdict_idxs) <= 1:
        return events
    last_idx = verdict_idxs[-1]
    events[last_idx]["earlier_verdicts_count"] = len(verdict_idxs) - 1
    drop = set(verdict_idxs[:-1])
    return [ev for i, ev in enumerate(events) if i not in drop]


def _coalesce_user_actions(events: list[dict]) -> list[dict]:
    """Collapse runs of adjacent dismissal user_actions (Ignore / Done / Mute
    and their reverts) with no other event between them. Rule: keep only the
    latest; if the latest reverts the one immediately before it, drop both
    so the streak vanishes entirely. Non-dismissal events (including
    `visited` and `read_on_github`) break runs and pass through unchanged."""
    out: list[dict] = []
    i = 0
    while i < len(events):
        ev = events[i]
        action = (ev.get("payload") or {}).get("action") if ev.get("kind") == "user_action" else None
        if action not in _DISMISSAL_ACTIONS:
            out.append(ev)
            i += 1
            continue
        streak_end = i + 1
        while streak_end < len(events):
            nxt = events[streak_end]
            nxt_action = (
                (nxt.get("payload") or {}).get("action")
                if nxt.get("kind") == "user_action" else None
            )
            if nxt_action not in _DISMISSAL_ACTIONS:
                break
            streak_end += 1
        latest = events[streak_end - 1]
        latest_action = (latest.get("payload") or {}).get("action")
        if streak_end - i >= 2:
            prev_action = (events[streak_end - 2].get("payload") or {}).get("action")
            if _REVERT_OF.get(latest_action) == prev_action:
                i = streak_end
                continue
        out.append(latest)
        i = streak_end
    return out


def _is_comment_like(ev: dict) -> bool:
    """A `review` with state COMMENTED is a top-level review with no
    verdict, just prose — semantically the same as a regular comment
    from the timeline-summary perspective. APPROVED / CHANGES_REQUESTED
    / DISMISSED reviews carry distinct signal and stay as their own
    lines."""
    kind = ev.get("kind")
    if kind == "comment":
        return True
    if kind == "review" and (ev.get("payload") or {}).get("state") == "COMMENTED":
        return True
    return False


def _coalesce_comments(events: list[dict]) -> list[dict]:
    """Collapse runs of adjacent comment-like events (issue/PR comments
    plus review-with-state-COMMENTED) that fall within
    _COMMENT_COALESCE_GAP_SECS of each other into a single
    `comment_group` event. Other kinds break the run. The group's
    timestamp / age_text comes from the latest in the run so the line
    reads as "happened recently" not "happened a while ago"."""
    out: list[dict] = []
    i = 0
    while i < len(events):
        ev = events[i]
        if not _is_comment_like(ev):
            out.append(ev)
            i += 1
            continue
        group = [ev]
        j = i + 1
        while (j < len(events)
               and _is_comment_like(events[j])
               and (events[j]["at_ts"] - events[j - 1]["at_ts"])
                   < _COMMENT_COALESCE_GAP_SECS):
            group.append(events[j])
            j += 1
        if len(group) == 1:
            out.append(ev)
        else:
            # Distinct authors in first-appearance order, each carrying
            # the badge_class derived during _format_event_for_render so
            # the template can render their inline icon.
            authors: list[dict] = []
            seen: set[str] = set()
            for c in group:
                a = (c.get("payload") or {}).get("author")
                if a and a not in seen:
                    seen.add(a)
                    authors.append({
                        "login":       a,
                        "badge_class": c.get("author_badge_class", ""),
                    })
            shown = authors[:3]
            extra = max(0, len(authors) - len(shown))
            last = group[-1]
            out.append({
                "kind":           "comment_group",
                "source":         "github",
                "at_ts":          last["at_ts"],
                "age_text":       last["age_text"],
                "payload":        {},
                "actor":          "GitHub",
                "count":          len(group),
                "shown_authors":  shown,
                "extra_authors":  extra,
            })
        i = j
    return out


def _format_event_for_render(
    row, now: int, user_login: str | None = None, *,
    cur_repo: str | None = None,
    tracked_people=frozenset(), notes_people: dict | None = None,
) -> dict:
    """Convert one thread_events row into a display-ready dict for the
    popover timeline. Each kind gets an `actor` (display name shown in
    the row gutter) and either a `summary` string (one-liner kinds) or
    full payload fields (ai_verdict / user_chat) for the chat-bubble
    rendering. comment / review events also pick up `author_badge_class`
    so the template can render the same icon styling the Repo column
    uses (self/member/first-time/generic). user_chat / ai_verdict bodies
    are run through ghmd for inline `code` / @mention / #ref rendering —
    `cur_repo` (the thread's "owner/name") resolves bare #NN refs."""
    try:
        payload = json.loads(row["payload_json"])
    except (ValueError, TypeError):
        payload = {}
    age_text = _humanize_age(now - row["ts"])
    kind = row["kind"]
    source = row["source"]

    out: dict = {
        "kind":     kind,
        "source":   source,
        "at_ts":    row["ts"],
        "age_text": age_text,
        "payload":  payload,
    }

    if kind == "comment":
        author = payload.get("author") or "?"
        out["actor"] = author
        out["author_badge_class"] = _author_badge_class(
            author, payload.get("author_association"), user_login,
        )
        out["summary"] = "commented"
    elif kind == "review":
        author = payload.get("author") or "?"
        out["actor"] = author
        out["author_badge_class"] = _author_badge_class(
            author, payload.get("author_association"), user_login,
        )
        state = (payload.get("state") or "").upper()
        label, cls = _REVIEW_STATE.get(state, (state.title() or "Reviewed", ""))
        out["review_state_label"] = label
        out["review_state_class"] = cls
    elif kind == "user_action":
        # GitHub observed the state change without us doing anything
        # locally; otherwise the user clicked something in our app.
        out["actor"] = "GitHub" if source == "github" else "You"
        action = payload.get("action") or "?"
        out["summary"] = _USER_ACTION_LABELS.get(action, action)
    elif kind == "lifecycle":
        actor = payload.get("actor") or "?"
        out["actor"] = actor
        # Lifecycle events come from GraphQL timelineItems and don't
        # carry author_association, so the chip falls through to the
        # generic icon (or 'self' when the actor is the user).
        out["author_badge_class"] = _author_badge_class(actor, None, user_login)
        action = payload.get("action") or "?"
        verb, cls = _LIFECYCLE_LABEL.get(action, (action, ""))
        reason = payload.get("reason")
        if reason and action == "closed":
            verb = f"{verb} ({reason.replace('_', ' ')})"
        out["lifecycle_verb"] = verb
        out["lifecycle_class"] = cls
    elif kind == "ai_verdict":
        out["actor"] = "AI"
        out["verdict"] = _verdict_render_dict(
            payload, cur_repo=cur_repo,
            tracked_people=tracked_people, notes_people=notes_people,
        )
    elif kind == "user_chat":
        out["actor"] = "You"
        out["body_html"] = ghmd.render(
            payload.get("body") or "", cur_repo=cur_repo, interactive=True,
            tracked_people=tracked_people, people_notes=notes_people,
        )
    elif kind == "body_edit":
        out["actor"] = payload.get("editor") or "?"
        out["summary"] = "edited the description"
    elif kind == "priority_change":
        out["actor"] = "You"
        to_val = payload.get("to")
        if isinstance(to_val, (int, float)):
            out["summary"] = f"set priority → {_score_to_priority_level(to_val).capitalize()}"
        elif to_val:  # legacy: band name stored directly
            out["summary"] = f"set priority → {str(to_val).capitalize()}"
        else:
            out["summary"] = "cleared priority (auto)"
    return out


# Event kinds that constitute "new context the AI hasn't seen" — a verdict
# made before any of these arrived is out of date. Row-state user_actions
# (read/done/mute) are the user's *response* to a verdict, not new context,
# so they don't count; neither does `visited`. (`code` pushes aren't here —
# they re-enable Re-ask via the verdict going `stale` when details
# re-enrich; `body_edit` is, since a reframed description is real new
# context.) Mirrors ai._THINKING_REQUIRED_KINDS.
_VERDICT_INVALIDATING_KINDS = ("comment", "review", "lifecycle", "user_chat", "body_edit")


def _recency_summary(rows, after: int, *, description_html) -> dict | None:
    """Non-AI "what's landed since the last verdict" recap for the AI-mode
    pill: GitHub activity (comments / reviews / lifecycle) with ts > `after`,
    summarised type-by-type in importance order (reviews, lifecycle, comments).
    None when there's nothing new — the pill segment is then absent. The hover
    repeats the verdict's standing take (`description_html`) above the
    breakdown so it reads as "the take, plus what's happened since."""
    comments = reviews = 0
    review_states: list[str] = []
    lifecycle_verbs: list[str] = []
    for r in rows:
        if r["ts"] <= after:
            continue
        kind = r["kind"]
        if kind == "comment":
            comments += 1
        elif kind == "review":
            reviews += 1
            try:
                st = (json.loads(r["payload_json"]).get("state") or "").upper()
            except (ValueError, TypeError):
                st = ""
            if st:
                review_states.append(st)
        elif kind == "lifecycle":
            try:
                action = json.loads(r["payload_json"]).get("action") or ""
            except (ValueError, TypeError):
                action = ""
            if action:
                lifecycle_verbs.append(_LIFECYCLE_LABEL.get(action, (action, ""))[0])
    if not (comments or reviews or lifecycle_verbs):
        return None

    def _plural(n: int, noun: str) -> str:
        return f"+{n} {noun}{'' if n == 1 else 's'}"

    parts: list[str] = []        # inline pill text — plain counts
    detail: list[str] = []       # hover line — counts + review-state notes
    if reviews:
        parts.append(_plural(reviews, "review"))
        cr = sum(1 for s in review_states if s == "CHANGES_REQUESTED")
        ap = sum(1 for s in review_states if s == "APPROVED")
        notes = [f"{cr} changes requested"] if cr else []
        if ap:
            notes.append(f"{ap} approved")
        detail.append(_plural(reviews, "review") + (f" ({', '.join(notes)})" if notes else ""))
    for verb in lifecycle_verbs:
        parts.append(verb)
        detail.append(verb)
    if comments:
        parts.append(_plural(comments, "comment"))
        detail.append(_plural(comments, "comment"))

    desc_part = (
        Markup('<div class="tip-recency-desc">{}</div>').format(description_html)
        if description_html else Markup("")
    )
    tip_html = desc_part + Markup('<div class="tip-recency-since">since the last assessment: {}</div>').format(
        ", ".join(detail))
    return {"text": " · ".join(parts), "tip_html": tip_html}


def _build_timeline(
    thread_id: str, conn: sqlite3.Connection, *,
    cur_repo: str | None = None,
    tracked_people=frozenset(), notes_people: dict | None = None,
) -> list:
    """The formatted, coalesced event list for the timeline popover. Lazily
    fetched (GET /timeline/<id>, on popover-open) rather than rendered into
    every table row: a busy table can carry thousands of thread_events, and
    their per-event markdown rendering would otherwise ride every table swap."""
    rows = conn.execute(
        """
        SELECT ts, kind, source, payload_json
          FROM thread_events
         WHERE thread_id = ?
         ORDER BY ts ASC, id ASC
        """,
        (thread_id,),
    ).fetchall()
    now = int(time.time())
    user_login = app.config.get("USER_LOGIN")
    timeline = [
        _format_event_for_render(
            r, now, user_login=user_login, cur_repo=cur_repo,
            tracked_people=tracked_people, notes_people=notes_people,
        )
        for r in rows
    ]
    # Order matters: every step that drops events runs before _coalesce_comments,
    # so a "judge after every comment" or "click through, mark read, archive"
    # workflow renders as one comment group rather than N chunks split around
    # noise events. Recurring user_action dedup runs before the dismissal-streak
    # coalesce so revert pairs (e.g. `done → undone`) become adjacent and cancel.
    # _mark_superseded_reviews last — it expects comment groups already formed
    # so the only reviews it sees are the state-bearing ones.
    timeline = _drop_close_after_merge(timeline)
    timeline = _drop_superseded_verdicts(timeline)
    timeline = _coalesce_recurring_actions(timeline)
    timeline = _coalesce_user_actions(timeline)
    timeline = _coalesce_body_edits(timeline)
    timeline = _coalesce_comments(timeline)
    timeline = _mark_superseded_reviews(timeline)
    return timeline


def _attach_verdict_status(d: dict, conn: sqlite3.Connection) -> None:
    """Mutate `d`: set `ai_uptodate` (drives the re-run button's enabled /
    green state and the row's outdated border) and `recency` (the AI pill's
    "+N reviews · +N comments since the last verdict" segment). Both derive
    only from thread_events newer than the cached verdict — a cheap slice;
    the full event list is fetched separately and lazily by _build_timeline."""
    verdict = d.get("ai_verdict")
    if not verdict:
        # No assessment yet — "not up to date" so the trigger button invites
        # the first Ask AI click; no verdict ⇒ no recency segment.
        d["ai_uptodate"] = False
        d["recency"] = None
        return
    after = verdict.get("at") or 0
    rows = conn.execute(
        """
        SELECT ts, kind, payload_json
          FROM thread_events
         WHERE thread_id = ? AND ts > ?
         ORDER BY ts ASC, id ASC
        """,
        (d["id"], after),
    ).fetchall()
    has_new_context = any(r["kind"] in _VERDICT_INVALIDATING_KINDS for r in rows)
    d["ai_uptodate"] = not verdict.get("stale") and not has_new_context
    d["recency"] = _recency_summary(rows, after, description_html=verdict.get("description_html"))


def _ai_verdict_dict(
    verdict_json: str | None, at: int | None, model: str | None,
    details_fetched_at: int | None, cur_repo: str | None = None,
) -> dict | None:
    """Parse the cached verdict + derive UI flags. None if no pending verdict.
    Stale = the row's details were re-enriched after the verdict was made,
    so the AI may have judged on outdated state. The user can re-ask before
    approving."""
    if not verdict_json or not at:
        return None
    try:
        verdict = json.loads(verdict_json)
    except (ValueError, TypeError):
        return None
    action_now = verdict.get("action_now") or "look"
    set_tracked = verdict.get("set_tracked") or "leave"

    priority_level, priority_score = _verdict_priority(verdict)

    # snooze_days rides with either snooze flavour; validate it to the route's
    # accepted range so the picker shortcut can post it safely. snooze_quiet
    # (action_now == "snooze_quiet") pre-checks the "unsubscribe while snoozed"
    # toggle in the snooze popover (the user can still untick it).
    is_snooze = action_now in ("snooze", "snooze_quiet")
    sd = verdict.get("snooze_days")
    snooze_days = sd if (is_snooze and isinstance(sd, int) and 1 <= sd <= 90) else None
    snooze_quiet = action_now == "snooze_quiet"

    age_text = _humanize_age(int(time.time()) - at)

    description = (verdict.get("description") or "").strip()

    # subscription_changes: list of mute_<kind> / unmute_<kind> tokens. Split
    # into per-direction kind lists (ordered by MUTE_KINDS, deduped); the
    # type-restriction + "is this still pending" computation happens in
    # _row_to_dict, which has the row's type and current muted_kinds.
    mute_suggested: list[str] = []
    unmute_suggested: list[str] = []
    sc = verdict.get("subscription_changes")
    if isinstance(sc, list):
        for tok in sc:
            if not isinstance(tok, str):
                continue
            verb, _, kind = tok.partition("_")
            if kind not in MUTE_KINDS:
                continue
            if verb == "mute":
                mute_suggested.append(kind)
            elif verb == "unmute":
                unmute_suggested.append(kind)
    mute_suggested = [k for k in MUTE_KINDS if k in mute_suggested]
    unmute_suggested = [k for k in MUTE_KINDS if k in unmute_suggested]

    return {
        "verdict":         verdict,
        "action_now":      action_now,
        "snooze_days":     snooze_days,   # None unless action_now is a snooze flavour
        "snooze_quiet":    snooze_quiet,  # True iff action_now == "snooze_quiet"
        "set_tracked":     set_tracked,
        "priority_level":  priority_level,
        "priority_score":  priority_score,
        "description":     description,
        # Subscription suggestion — refined (type-restricted, pending flags,
        # summary string) in _row_to_dict.
        "mute_suggested":   mute_suggested,
        "unmute_suggested": unmute_suggested,
        # Non-interactive: the AI button's hover tooltip can't host clickable
        # links (it dismisses on mouse-out), so refs/mentions there render as
        # styled spans, not anchors. `code` still renders.
        "description_html": ghmd.render(description, cur_repo=cur_repo, interactive=False),
        "model":           model or "",
        "at":              at,
        "age_text":        age_text,
        "stale":           bool(details_fetched_at and details_fetched_at > at),
    }


def _row_to_dict(
    row,
    tracked_people: set[str] | None = None,
    tracked_repos: set[str] | None = None,
    tracked_orgs: set[str] | None = None,
    notes_people: dict[str, str] | None = None,
    notes_repos: dict[str, str] | None = None,
    notes_orgs: dict[str, str] | None = None,
) -> dict:
    d = dict(row)
    details_json = d.pop("details_json", None)
    details_fetched_at = d.pop("details_fetched_at", None)
    seen_reasons_json = d.pop("seen_reasons", None)
    baseline_comments = d.pop("baseline_comments", None)
    ai_verdict_json = d.pop("ai_verdict_json", None)
    ai_verdict_at = d.pop("ai_verdict_at", None)
    ai_verdict_model = d.pop("ai_verdict_model", None)
    repo_owner, repo_name = _split_repo(d["repo"])

    details: dict = {}
    if details_json:
        try:
            details = json.loads(details_json)
        except (ValueError, TypeError):
            pass
    seen: set[str] = set()
    if seen_reasons_json:
        try:
            seen = set(json.loads(seen_reasons_json))
        except (ValueError, TypeError):
            pass

    # Per-thread muted notification kinds — render order follows MUTE_KINDS;
    # the ▾ menu only offers the kinds that can actually fire for this type.
    muted_kinds_json = d.pop("muted_kinds", None)
    muted_kinds: list[str] = []
    if muted_kinds_json:
        try:
            stored = set(json.loads(muted_kinds_json))
            muted_kinds = [k for k in MUTE_KINDS if k in stored]
        except (ValueError, TypeError):
            pass
    d["muted_kinds"] = muted_kinds
    d["mute_kind_options"] = _mute_kind_options(d["type"])

    d["meta"] = _format_meta(
        details_json,
        d["type"],
        baseline_comments,
        d.pop("pr_reactions_json", None),
        d.pop("unique_commenters", None),
        d.pop("unique_reviewers", None),
    )
    d["age"] = _age_pill(details.get("created_at"), d["type"]) if details else None
    d["created_at"] = (details.get("created_at") if details else None) or ""
    d["popularity"] = _popularity_pill(d["meta"].get("reception"))
    all_labels = _extract_labels(details_json)
    d["labels_visible"] = all_labels[:3]
    d["labels_extra"] = all_labels[3:]
    d["type_label"] = TYPE_LABELS.get(d["type"], d["type"])
    d["type_label_long"] = TYPE_LABELS_LONG.get(d["type"], d["type"])
    # Issue/PR titles render `code` spans on github.com but nothing else.
    d["title_html"] = ghmd.render_title(d["title"])
    # Discussion category (Q&A, Ideas, Show and tell, …) renders as a
    # separate pill in the meta row alongside labels. GraphQL exposes no
    # color for categories, so the pill stays neutral.
    d["category"] = details.get("category") if d["type"] == "Discussion" else None
    d["type_state"] = _type_state(details, d["type"])
    # Bucket is set later in _filter_and_sort once the active sort is
    # known — the time field we bucket by depends on it. Initialize to
    # None so callers that bypass _filter_and_sort (e.g. _load_one for
    # single-row swaps) get a defined value; bucketing only matters in
    # the table view anyway.
    d["bucket"] = None
    d["repo_owner"], d["repo_name"] = repo_owner, repo_name
    d["action_needed"] = _action_needed(details, repo_owner, d["reason"], seen)
    d["mentioned_since"] = _mentioned_since(seen)
    d["is_author"] = _is_author(details)
    # "Involved" = directed at you or an active participant, by any reason
    # GitHub has ever delivered for this thread (current or accumulated), plus
    # the enriched author check (catches a row not yet enriched-as-yours).
    d["is_involved"] = bool(
        d["is_author"]
        or (d["reason"] or "") in github.INVOLVED_REASONS
        or (seen & github.INVOLVED_REASONS)
    )
    d["merge_state"] = _merge_state(details, d["type"])
    # 'New since last action': review state changed since the user last engaged
    # (action or first ingest, baseline NULL means "never engaged").
    baseline_rs = d.pop("baseline_review_state", None)
    d["is_review_new"] = bool(d["pr_review_state"]) and d["pr_review_state"] != baseline_rs
    d["thread_pill"] = _thread_pill(d)

    # Author info — pulled from cached details_json; null if not yet enriched.
    author = (details.get("user") or {}) if details else {}
    d["author_login"] = author.get("login") or ""
    d["author_assoc"] = (details.get("author_association") if details else None) or ""
    badge = _AUTHOR_BADGE.get(d["author_assoc"])
    d["author_badge_class"] = badge[0] if badge else ""
    d["author_badge_label"] = badge[1] if badge else ""
    d["author_is_tracked"] = bool(tracked_people) and d["author_login"] in (tracked_people or set())
    d["repo_is_tracked"] = d["repo"] in (tracked_repos or set())
    d["org_is_tracked"] = bool(repo_owner) and repo_owner in (tracked_orgs or set())
    d["author_note"] = (notes_people or {}).get(d["author_login"]) if d["author_login"] else None
    d["repo_note"] = (notes_repos or {}).get(d["repo"])
    d["org_note"] = (notes_orgs or {}).get(repo_owner) if repo_owner else None

    # Cached AI verdict (None if Ask AI hasn't been run on this row).
    d["ai_verdict"] = _ai_verdict_dict(
        ai_verdict_json, ai_verdict_at, ai_verdict_model, details_fetched_at,
        cur_repo=d["repo"],
    )
    # Refine the verdict's subscription suggestion against this row's reality:
    # restrict to the kinds that apply to its type, work out which suggested
    # changes are still *pending* (not yet matched by muted_kinds), and build a
    # human summary for the caret tooltip / apply button. The UI only flags a
    # suggestion while it mismatches — once the user has applied it, the
    # purple cues clear (so they never read as an ambiguous "toggle this").
    if d["ai_verdict"]:
        av = d["ai_verdict"]
        applicable = {k for k, _, mutable in d["mute_kind_options"] if mutable}
        muted_now = set(d["muted_kinds"])
        ms = [k for k in av["mute_suggested"] if k in applicable]
        us = [k for k in av["unmute_suggested"] if k in applicable]
        pending_mute = [k for k in ms if k not in muted_now]
        pending_unmute = [k for k in us if k in muted_now]
        av["mute_suggested"] = ms
        av["unmute_suggested"] = us
        av["pending_mute"] = pending_mute
        av["pending_unmute"] = pending_unmute
        av["has_subscription_suggestion"] = bool(ms or us)
        av["subscription_pending"] = bool(pending_mute or pending_unmute)
        av["subscription_summary"] = ", ".join(
            [f"mute {_MUTE_KIND_LABEL.get(k, k).lower()}" for k in ms]
            + [f"unmute {_MUTE_KIND_LABEL.get(k, k).lower()}" for k in us]
        )

        # action_now / set_tracked surface as a purple ring on the matching
        # Actions-column button — but only while the suggestion isn't already
        # in effect (so the ring always reads "do this", never "undo this").
        # `look` has no button (its affordance is the title link), so it never
        # rings anything. Both snooze flavours ring the one Snooze button — once
        # the row is snoozed (either flavour) we stop nagging; the quiet/loud
        # distinction surfaces as the pre-checked "unsubscribe" toggle inside
        # the popover (av["snooze_quiet"]), not the ring.
        action = av["action_now"]
        action_in_effect = {
            "ignore":  (not d["unread"]) and (not d["ignored"])
                       and d["action"] not in ("done", "snoozed"),
            "archive": d["action"] == "done" and not d["ignored"],
            "mute":    d["action"] == "done" and bool(d["ignored"]),
            "snooze":       bool(d.get("snooze_until")),
            "snooze_quiet": bool(d.get("snooze_until")),
        }.get(action, True)  # 'look' / unknown → treat as "nothing to ring"
        av["action_pending"] = action if not action_in_effect else None
        st = av["set_tracked"]
        track_in_effect = (st == "track" and bool(d["is_tracked"])) \
            or (st == "untrack" and not d["is_tracked"]) \
            or st not in ("track", "untrack")
        av["track_pending"] = st if not track_in_effect else None

    # Effective priority — a 0..1 float. The user's hand-pin wins (it only
    # persists until the next verdict, which clears priority_user — see
    # ai._save_verdict); otherwise fall back to the cached verdict's score.
    # priority_score drives the --imp gradient; priority_level is the band it
    # falls in, used to highlight a cell in the picker (same look whether the
    # value is a user pin or the AI's).
    priority_user = _clamp01(d.get("priority_user"))
    d["priority_user"] = priority_user
    if priority_user is not None:
        d["priority_score"] = priority_user
        d["priority_level"] = _score_to_priority_level(priority_user)
    elif d["ai_verdict"]:
        d["priority_score"] = d["ai_verdict"]["priority_score"]
        d["priority_level"] = d["ai_verdict"]["priority_level"]
    else:
        d["priority_score"] = None
        d["priority_level"] = None

    # Snooze — `snooze_until` (raw wake ts, or None) is already in `d`; add a
    # short "wakes in 3d" label for the in-row snoozed-state button.
    su = d.get("snooze_until")
    d["snooze_wakes_in"] = _short_duration(su - int(time.time())) if su else None
    # Quiet snooze (also unsubscribed for the duration) — `ignored` is the
    # discriminator; the row button reads/labels differently.
    d["snooze_quiet"] = bool(su) and bool(d["ignored"])

    return d


def _load_notifications(show_archived: bool = True):
    """Load + hydrate notification rows for the table. `_filter_and_sort`
    remains the source of truth for visibility; `show_archived=False` just
    lets the default view skip loading (and hydrating) the archived/snoozed
    rows it would immediately discard — pass `f["show_archived"]` through."""
    t_p = _tracked_people()
    t_r = _tracked_set("repos")
    t_o = _tracked_set("orgs")
    n_p = _entity_notes("people", "login")
    n_r = _entity_notes("repos", "name")
    n_o = _entity_notes("orgs", "name")
    conn = db.connect()
    try:
        where = "" if show_archived else "WHERE COALESCE(action, '') NOT IN ('done', 'snoozed') "
        rows = conn.execute(
            f"SELECT {_ROW_COLS} FROM notifications {where}"
            "ORDER BY COALESCE(effective_updated_at, updated_at) DESC"
        ).fetchall()
        out = [_row_to_dict(r, t_p, t_r, t_o, n_p, n_r, n_o) for r in rows]
        # The timeline popover's event list is fetched lazily (GET /timeline
        # /<id> on open) — see _build_timeline. Eagerly attach only the cheap
        # verdict-derived bits the row itself shows (pill recency, re-run state).
        for d in out:
            _attach_verdict_status(d, conn)
        return out
    finally:
        conn.close()


def _load_repo_options(show_archived: bool = False) -> tuple[list[str], list[str]]:
    """Distinct owners and repo names from notifications. Drives the Owner
    and Repo filter dropdowns. Names are de-duplicated across owners — if
    'godot' shows up under two owners it appears once. Archived rows are
    excluded unless `show_archived` is set, so the dropdowns mirror what's
    visible in the table under the active filter."""
    conn = db.connect()
    try:
        if show_archived:
            rows = conn.execute(
                "SELECT DISTINCT repo FROM notifications WHERE repo != ''"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT DISTINCT repo FROM notifications "
                "WHERE COALESCE(action, '') NOT IN ('done', 'snoozed') AND repo != ''"
            ).fetchall()
    finally:
        conn.close()
    owners: set[str] = set()
    names: set[str] = set()
    for r in rows:
        owner, name = _split_repo(r["repo"])
        if owner:
            owners.add(owner)
        if name:
            names.add(name)
    return sorted(owners), sorted(names)


def _filters_from_request() -> dict:
    src = request.values  # union of query string + form fields
    return {
        "actions":       src.getlist("actions"),
        "hide_read":     bool(src.get("hide_read")),
        "hide_resolved": bool(src.get("hide_resolved")),
        "show_archived": bool(src.get("show_archived")),
        "tracked_only":  bool(src.get("tracked_only")),
        "mine_only":     bool(src.get("mine_only")),
        "involved_only": bool(src.get("involved_only")),
        "owner":         src.get("owner") or "",
        "repo":          src.get("repo") or "",
        "sort":          src.get("sort") or "updated",
        "q":             (src.get("q") or "").strip(),
        "types":         src.getlist("types"),
    }


# Triage mode (manual / ai), the quiet-bystanders toggle, and the
# auto-refresh cadence all live in config/settings.toml — see app/settings.py
# for the spec and migration. Kept as thin wrappers here so call sites read
# the same way they used to.
def _get_triage_mode() -> str:
    return settings.get("triage_mode")


def _set_triage_mode(mode: str) -> str:
    try:
        return settings.set("triage_mode", mode)
    except ValueError:
        return settings.set("triage_mode", "manual")


def _get_quiet_bystanders() -> bool:
    return settings.get("quiet_bystanders")


def _set_quiet_bystanders(on: bool) -> bool:
    coerced = settings.set("quiet_bystanders", bool(on))
    if not coerced:
        # Flush in-flight windows so the change is visible immediately
        # rather than waiting for each thread's window to expire.
        conn = db.connect()
        try:
            github.drain_throttle_windows(conn)
            conn.commit()
        finally:
            conn.close()
    return coerced


AUTO_REFRESH_MODES = ("live", "hourly", "daily", "manual")


def _get_auto_refresh() -> str:
    return settings.get("auto_refresh")


def _set_auto_refresh(mode: str) -> str:
    return settings.set("auto_refresh", mode)


def _set_last_poll_at(epoch: int) -> None:
    """Record the timestamp of the most recent successful GitHub poll —
    read by poll.run_loop to decide whether the next scheduled tick is due,
    and surfaced (via auto_refresh mode + this stamp) to the status pill."""
    conn = db.connect()
    try:
        db.set_meta(conn, "last_poll_at", str(int(epoch)))
        conn.commit()
    finally:
        conn.close()


def _render_row(n: dict):
    """Wrap render_template('_row.html', ...) so every row swap carries
    the active triage_mode (persisted setting) and sort (rides on the
    request via the hx-included #filters — the Reception column shows the
    engaged-count in AI mode only while that's the active sort)."""
    return render_template(
        "_row.html", n=n, triage_mode=_get_triage_mode(),
        sort=request.values.get("sort") or "updated",
    )


def _eff_updated(r: dict) -> str:
    """The local sort timestamp: effective_updated_at, which mirrors GitHub's
    updated_at except an absorbed muted-only re-delivery leaves it frozen so
    the row keeps its slot. Falls back to updated_at for safety."""
    return r.get("effective_updated_at") or r.get("updated_at") or ""


def _filter_and_sort(rows: list[dict], f: dict) -> list[dict]:
    # Archived (action='done') and snoozed (action='snoozed') rows are hidden
    # by default and only surface when Show archived is on. Distinct from Hide
    # resolved below, which filters by GitHub-side resolution state
    # (merged/closed/answered).
    if not f.get("show_archived"):
        rows = [r for r in rows if r["action"] not in ("done", "snoozed")]
    if f["actions"]:
        actions = set(f["actions"])
        want_mentioned = "mentioned" in actions
        rows = [
            r for r in rows
            if r["action_needed"] in actions
            or (want_mentioned and r["mentioned_since"])
        ]
    if f["hide_read"]:
        rows = [r for r in rows if r["unread"]]
    if f["hide_resolved"]:
        rows = [r for r in rows if r["type_state"] not in RESOLVED_TYPE_STATES]
    if f["tracked_only"]:
        rows = [
            r for r in rows
            if r["is_tracked"]
            or r["author_is_tracked"]
            or r["repo_is_tracked"]
            or r["org_is_tracked"]
        ]
    if f["mine_only"]:
        rows = [r for r in rows if r["is_author"]]
    if f["involved_only"]:
        rows = [r for r in rows if r["is_involved"]]
    if f["owner"]:
        rows = [r for r in rows if r["repo_owner"] == f["owner"]]
    if f["repo"]:
        rows = [r for r in rows if r["repo_name"] == f["repo"]]
    if f["types"]:
        types = set(f["types"])
        rows = [r for r in rows if r["type"] in types]
    if f["q"]:
        q = f["q"].lower()
        rows = [
            r for r in rows
            if q in r["title"].lower()
            or q in (r["author_login"] or "").lower()
        ]
    if f["sort"] == "priority":
        # Highest effective priority first (user pin, else AI verdict — see
        # _row_to_dict). Unassessed rows sort as if scored just below routine
        # (_UNASSESSED_SORT_SCORE) — they beat irrelevant + minor but lose to
        # anything explicitly pinned routine-or-above; presumed-low rather
        # than sunk to the bottom. Ties — rare for AI floats, routine among
        # the six user-pin values — break by actionable-to-you (assigned /
        # review-requested / mentioned), then most-recently-updated. Two
        # stable passes: the recency pass below is preserved within each
        # (score, actionable) group by the main sort.
        rows.sort(key=_eff_updated, reverse=True)
        rows.sort(key=lambda r: (
            -(r["priority_score"] if r["priority_score"] is not None else _UNASSESSED_SORT_SCORE),
            not (r["action_needed"] or r["mentioned_since"]),
        ))
    elif f["sort"] == "engaged":
        rows.sort(
            key=lambda r: (
                ((r.get("meta") or {}).get("interest") or {}).get("engaged") or 0
            ),
            reverse=True,
        )
    elif f["sort"] == "stale":
        # Oldest-updated first. Pairs with Hide resolved to surface forgotten
        # open work; on its own, surfaces both forgotten and long-resolved.
        rows.sort(key=_eff_updated)
    elif f["sort"] == "oldest":
        # Oldest-created first. Differs from 'stale': a long-running issue
        # that just got a comment is old here but fresh by stale's measure.
        # Items not yet enriched (no created_at) sink to the bottom rather
        # than bubble to the top with an empty-string sort key.
        rows.sort(key=lambda r: r["created_at"] or "9999")
    elif f["sort"] == "newest":
        # Newest-created first. Differs from 'updated' ("Most recent"): an
        # ancient issue that just got a comment is most-recent but not newest.
        # Empty-string created_at sinks to the bottom in this reverse sort.
        rows.sort(key=lambda r: r["created_at"], reverse=True)

    # Bucket separators reflect the active sort: temporal sorts bucket by
    # the same time key they sort on, so the "Today / Yesterday / ..."
    # headers are honest about what they're grouping; the priority sort
    # buckets by named band ("Urgent" / "High" / ... / "Unassessed").
    # Engagement gets no bucketing — the order carries no group semantics.
    sort = f["sort"]
    if sort in ("updated", "stale"):
        for r in rows:
            r["bucket"] = _bucket(_eff_updated(r))
    elif sort in ("newest", "oldest"):
        for r in rows:
            r["bucket"] = _bucket(r["created_at"])
    elif sort == "priority":
        for r in rows:
            s = r["priority_score"]
            r["bucket"] = "Unassessed" if s is None else _score_to_priority_level(s).capitalize()
    # else: leave bucket as None (set in _row_to_dict) → template skips.
    return rows


def _load_one(thread_id: str) -> dict | None:
    t_p = _tracked_people()
    t_r = _tracked_set("repos")
    t_o = _tracked_set("orgs")
    n_p = _entity_notes("people", "login")
    n_r = _entity_notes("repos", "name")
    n_o = _entity_notes("orgs", "name")
    conn = db.connect()
    try:
        row = conn.execute(
            f"SELECT {_ROW_COLS} FROM notifications WHERE id = ?",
            (thread_id,),
        ).fetchone()
        if not row:
            return None
        d = _row_to_dict(row, t_p, t_r, t_o, n_p, n_r, n_o)
        _attach_verdict_status(d, conn)
        return d
    finally:
        conn.close()


@app.get("/")
def index():
    f = _filters_from_request()
    rows = _filter_and_sort(_load_notifications(f["show_archived"]), f)
    owners, repo_names = _load_repo_options(show_archived=f["show_archived"])
    return render_template(
        "index.html",
        notifications=rows,
        owners=owners,
        repo_names=repo_names,
        filters=f,
        sort=f["sort"],
        triage_mode=_get_triage_mode(),
        quiet_bystanders=_get_quiet_bystanders(),
        auto_refresh=_get_auto_refresh(),
        type_labels=TYPE_LABELS_LONG,
        action_labels=ACTION_FILTER_LABELS,
        event_seq=events.current_seq(),
    )


@app.get("/list")
def list_view():
    """Re-render the table with current filter/sort params (no polling)."""
    f = _filters_from_request()
    rows = _filter_and_sort(_load_notifications(f["show_archived"]), f)
    return render_template(
        "_table.html", notifications=rows, error=None, filters=f, sort=f["sort"],
        triage_mode=_get_triage_mode(),
    )


@app.get("/timeline/<thread_id>")
def thread_timeline(thread_id: str):
    """Render just the popover's event-list <li>s. The row template ships the
    <ol> empty; HTMX fetches this on popover-open (re-fetched each open, so a
    new chat / verdict / activity shows without a table refresh). Keeps the
    formatted, markdown-rendered timeline out of every table swap — the win
    behind making the popover lazy. Renders in both triage modes."""
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT repo FROM notifications WHERE id = ?", (thread_id,)
        ).fetchone()
        if not row:
            return ("", 404)
        timeline = _build_timeline(
            thread_id, conn, cur_repo=row["repo"],
            tracked_people=_tracked_people(),
            notes_people=_entity_notes("people", "login"),
        )
    finally:
        conn.close()
    return render_template("_timeline_list.html", timeline=timeline, thread_id=thread_id)


@app.post("/settings/quiet_bystanders")
def set_quiet_bystanders():
    """Toggle the bystander-thread throttle (see github._apply_throttle).
    Doesn't retroactively alter the current view — only affects how future
    polls treat bursts — so it returns 204 and the dropdown row flips its
    own checkmark client-side."""
    requested = (request.values.get("on") or "").strip().lower()
    if requested in ("1", "true", "on"):
        new = True
    elif requested in ("0", "false", "off"):
        new = False
    else:
        new = not _get_quiet_bystanders()
    _set_quiet_bystanders(new)
    return ("", 204)


@app.post("/settings/auto_refresh")
def set_auto_refresh():
    """Persist the auto-refresh cadence (live / hourly / daily / manual).
    The change affects future polls only (poll.run_loop reads the value each
    iteration), and the dropdown row flips its own radio mark client-side —
    so 204, no table re-render. Invalid values 400."""
    mode = (request.values.get("value") or "").strip()
    try:
        _set_auto_refresh(mode)
    except ValueError as e:
        return (str(e), 400)
    return ("", 204)


@app.post("/settings/triage_mode")
def set_triage_mode():
    """Persist the Relevance-column mode (manual vs ai) and re-render the
    table so the per-row Relevance cells reflect the change. The toggle row
    in the toolbar's Options menu flips its own checkmark client-side. Body
    field 'mode' is optional — when omitted, flip the current value."""
    requested = (request.values.get("mode") or "").strip()
    new_mode = requested if requested in ("manual", "ai") else (
        "manual" if _get_triage_mode() == "ai" else "ai"
    )
    _set_triage_mode(new_mode)
    f = _filters_from_request()
    rows = _filter_and_sort(_load_notifications(f["show_archived"]), f)
    return render_template(
        "_table.html", notifications=rows, error=None, filters=f, sort=f["sort"],
        triage_mode=new_mode,
    )


def _table_response(error: str | None) -> "Response":
    """Re-render the table with current filters; if an error happened, attach
    HX-Trigger 'showError' so the status dot flips red without disrupting the swap."""
    f = _filters_from_request()
    rows = _filter_and_sort(_load_notifications(f["show_archived"]), f)
    body = render_template(
        "_table.html", notifications=rows, filters=f, sort=f["sort"],
        triage_mode=_get_triage_mode(),
    )
    response = make_response(body, 200)
    if error:
        response.headers["HX-Trigger"] = json.dumps({"showError": {"message": error}})
    return response


@app.post("/refresh")
def refresh():
    token = app.config["GITHUB_TOKEN"]
    error: str | None = None
    # ?auto=1 marks browser-driven refreshes (SSE-triggered) so they ride the
    # poll predicate and skip the unread fetch on a quiet inbox. A user-
    # clicked refresh has no flag and forces a full sync so the click never
    # feels like it missed something.
    auto = bool(request.values.get("auto"))
    force_full = not auto
    conn = db.connect()
    try:
        try:
            github.poll_once(conn, token, force_full=force_full)
        except Exception as e:
            log.exception("on-demand refresh failed")
            error = f"Refresh failed: {e}"
        if not error:
            # Stamp the successful poll so poll.run_loop sees the cadence
            # window as freshly satisfied and doesn't double-fire on its
            # next wake. A failed poll leaves last_poll_at untouched so the
            # loop can retry on its own schedule.
            db.set_meta(conn, "last_poll_at", str(int(time.time())))
            conn.commit()
        # Fingerprint-gated SSE bump. notify_if_changed returns True iff the
        # render inputs actually moved — also tells us whether this auto-
        # refresh has anything to swap. 204 leaves an open popover (and any
        # in-progress edit) untouched.
        changed = events.notify_if_changed(conn)
        if auto and not error and not changed:
            return make_response("", 204)
    finally:
        conn.close()
    return _table_response(error)


@app.get("/events")
def sse_events():
    """SSE channel: emits `data: <seq>` whenever the poll loop (or another
    refresh handler) bumps the global counter, plus a `: ping` comment every
    ~15s so idle connections survive proxy timeouts. The first message after
    open is the current seq — clients should use it to seed their last-seen
    state, not refresh on it, since the page itself was rendered from the
    same DB the seq describes."""
    def gen():
        last_seen = events.current_seq()
        yield f"data: {last_seen}\n\n"
        while True:
            seq = events.wait(last_seen, timeout=15.0)
            if seq > last_seen:
                last_seen = seq
                yield f"data: {seq}\n\n"
            else:
                yield ": ping\n\n"
    resp = Response(gen(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"  # disable proxy buffering
    return resp


@app.post("/backfetch")
def backfetch():
    """Backfill last N notifications (incl read) and force re-enrichment.
    N is clamped to [1, 1000] to keep API budget bounded."""
    try:
        n = int(request.values.get("n", 50))
    except (TypeError, ValueError):
        n = 50
    n = max(1, min(n, 1000))
    token = app.config["GITHUB_TOKEN"]
    error: str | None = None
    conn = db.connect()
    try:
        try:
            github.backfetch(conn, token, n=n)
        except Exception as e:
            log.exception("backfetch failed")
            error = f"Backfill ({n}) failed: {e}"
    finally:
        conn.close()
    return _table_response(error)


@app.post("/backfetch/issues")
def backfetch_issues():
    """Backfill open issues/PRs from the search API as synthetic rows.
    scope ∈ {authored (author:@me), involved (involves:@me)}; latest N by
    updated, N clamped to [1, 1000]."""
    scope = request.values.get("scope", "")
    if scope not in ("authored", "involved"):
        return _table_response("Backfill: unknown scope")
    try:
        n = int(request.values.get("n", 50))
    except (TypeError, ValueError):
        n = 50
    n = max(1, min(n, 1000))
    token = app.config["GITHUB_TOKEN"]
    error: str | None = None
    conn = db.connect()
    try:
        try:
            github.backfetch_issues(conn, token, scope, n=n)
        except Exception as e:
            log.exception("backfetch_issues failed")
            error = f"Backfill ({scope} {n}) failed: {e}"
    finally:
        conn.close()
    return _table_response(error)


@app.post("/track-link")
def track_link():
    """Add a synthetic row for a pasted github.com issue / PR / discussion URL
    (the single-item cousin of /backfetch/issues). No-op if a row already
    covers that thread."""
    url = (request.values.get("url") or "").strip()
    if not url:
        return _table_response("Paste a GitHub issue / PR / discussion link")
    token = app.config["GITHUB_TOKEN"]
    error: str | None = None
    conn = db.connect()
    try:
        try:
            github.track_link(conn, token, url)
        except ValueError as e:
            error = str(e)
        except Exception as e:
            log.exception("track-link failed")
            error = f"Couldn't add link: {e}"
    finally:
        conn.close()
    return _table_response(error)


def _apply_action(
    thread_id: str,
    action: str,
    source: str = "user",
    log_action: str | None = None,
    event_payload: dict | None = None,
    **state,
) -> None:
    """Record action + actioned_at + action_source, plus arbitrary state columns.

    `action` is what lands in `notifications.action` — the filter-relevant
    label (e.g. 'done' hides the row by default). `log_action` overrides the
    value written into `thread_events.payload.action`, so the AI timeline can
    record a more specific label ('muted') while the row column stays 'done'
    for filter purposes. Defaults to `action` when unset. `event_payload`
    merges extra keys into that timeline payload (e.g. the wake time on a
    `snoozed` event).

    `snooze_until` is reset to NULL by default — any row action other than
    the snooze handler itself un-snoozes the row — but a caller can pass
    `snooze_until=<ts>` via `**state` to set it.

    NOTE: baselines (baseline_comments, baseline_review_state) and seen_reasons
    are intentionally NOT touched here. 'Since last looked' indicators persist
    through Read so the user can see what they just handled; only fresh
    notification activity (new comment count, new review state, new mention)
    shifts them. Done/Unsub remove the row from view, so any staleness there
    is invisible.
    """
    now = int(time.time())
    cols = {
        "action": action,
        "actioned_at": now,
        "action_source": source,
        "snooze_until": None,
        **state,
    }
    setters = ", ".join(f"{k} = ?" for k in cols)
    values = (*cols.values(), thread_id)
    conn = db.connect()
    try:
        conn.execute(
            f"UPDATE notifications SET {setters} WHERE id = ?", values
        )
        # Mirror to thread_events so the AI sees user-side activity in the
        # per-thread timeline. external_id is NULL — every click is a real
        # event and shouldn't dedup with prior identical actions.
        db.write_thread_event(
            conn,
            thread_id=thread_id,
            ts=now,
            kind="user_action",
            source=source,
            payload={"action": log_action or action, **(event_payload or {})},
        )
    finally:
        conn.close()


@app.post("/visit/<thread_id>")
def visit(thread_id: str):
    """User clicked the external link to the underlying GitHub thread.
    Always log a `visited` user_action event so the AI sees the engagement
    distinct from a plain mark-read click (which means "dismissed without
    opening"). State side effects mirror the prior link-click behavior
    that used /set/<id>/ignored: mark read on GitHub if unread; unsubscribe
    if ignored (visiting a muted thread unmutes it). Returns the
    re-rendered row so the read pill updates in place."""
    token = app.config["GITHUB_TOKEN"]
    n = _load_one(thread_id)
    if not n:
        return ("", 404)
    needs_state_change = bool(n["unread"]) or bool(n["ignored"])
    if n["unread"]:
        github.mark_read(token, thread_id)
    if n["ignored"]:
        github.set_subscribed(token, thread_id)
    if needs_state_change:
        # _apply_action writes the thread_events row AND updates the
        # notifications columns; one-shot for the state-changing path.
        _apply_action(thread_id, "visited", unread=0, ignored=0)
    else:
        # Already in clean read state — log the visit alone, no
        # notifications.action overwrite (preserves any prior label
        # like 'muted' or 'kept_unread' as the row's last state).
        conn = db.connect()
        try:
            db.write_thread_event(
                conn,
                thread_id=thread_id,
                ts=int(time.time()),
                kind="user_action",
                source="user",
                payload={"action": "visited"},
            )
        finally:
            conn.close()
    return _render_row(_load_one(thread_id))


# The three action buttons in the Actions column are radio-style:
#   Ignore  = marked read but kept visible (unread=0, ignored=0, action!='done')
#   Done    = archived on GitHub but stays subscribed (action='done', ignored=0)
#   Mute    = archived AND unsubscribed (action='done', ignored=1)
#
# Each toggles: click the active button to revert. Revert is local-only when
# the underlying GitHub state is one-way (mark-read, mark-done) — the column
# values flip back but GitHub stays where it was. Subscription is reversible
# via set_subscribed.
#
# Done/Mute hide the row by default (the show_archived filter is what surfaces
# action='done' rows). When the form's `show_archived` is on, the row stays in
# the DOM and re-renders with the new active button; otherwise the response is
# empty so HTMX swaps the <tr> away.

def _archive_response(thread_id: str):
    """Return an updated row when the user has Show archived on, otherwise an
    empty body so hx-swap=outerHTML removes the <tr>. Used by Done / Mute,
    where the new state (action='done') is filter-hidden in the default view."""
    if request.values.get("show_archived"):
        return _render_row(_load_one(thread_id))
    return ("", 200)


@app.post("/set/<thread_id>/ignored")
def set_ignored(thread_id: str):
    """Toggle Ignore: marked read + visible (the default 'I've seen this'
    state). Click again to deselect back to unread. Also clears any
    pre-existing done / muted state — Ignore brings a row back into the
    default view."""
    token = app.config["GITHUB_TOKEN"]
    n = _load_one(thread_id)
    if not n:
        return ("", 404)
    is_active = (
        n["unread"] == 0
        and n["ignored"] == 0
        and n["action"] not in ("done", "snoozed")
    )
    if is_active:
        # Click already-active Ignore → deselect → mark unread locally.
        # GitHub REST has no 'mark unread' so this stays a local marker;
        # action='kept_unread' tells the reconciler not to flip it back.
        _apply_action(thread_id, "kept_unread", unread=1, ignored=0)
    else:
        if n["unread"]:
            github.mark_read(token, thread_id)
        if n["ignored"]:
            github.set_subscribed(token, thread_id)
        # 'read' clears the action column from any prior 'done', so the row
        # rejoins the default view. GitHub keeps its archive flag — new
        # activity on the thread will resurface it naturally.
        _apply_action(thread_id, "read", unread=0, ignored=0)
    return _render_row(_load_one(thread_id))


@app.post("/set/<thread_id>/done")
def set_done(thread_id: str):
    """Toggle Done: archive on GitHub (one-way). Click again to revert
    locally — GitHub stays archived, but the row resurfaces in the default
    view. From a muted state, Done resubscribes but keeps the archive."""
    token = app.config["GITHUB_TOKEN"]
    n = _load_one(thread_id)
    if not n:
        return ("", 404)
    is_active = (n["action"] == "done" and not n["ignored"])
    if is_active:
        _apply_action(thread_id, "undone")
        return _render_row(_load_one(thread_id))
    if n["ignored"]:
        github.set_subscribed(token, thread_id)
    if n["action"] != "done":
        github.mark_done(token, thread_id)
    _apply_action(thread_id, "done", unread=0, ignored=0)
    return _archive_response(thread_id)


@app.post("/set/<thread_id>/muted")
def set_muted(thread_id: str):
    """Toggle Mute: archive on GitHub AND unsubscribe so future activity on
    the thread won't resurface it. Click again to revert — set_subscribed
    is reversible, mark_done isn't (GitHub stays archived but the row
    resurfaces locally)."""
    token = app.config["GITHUB_TOKEN"]
    n = _load_one(thread_id)
    if not n:
        return ("", 404)
    is_active = (n["action"] == "done" and n["ignored"])
    if is_active:
        github.set_subscribed(token, thread_id)
        _apply_action(thread_id, "unmuted", ignored=0)
        return _render_row(_load_one(thread_id))
    if n["action"] != "done":
        github.mark_done(token, thread_id)
    if not n["ignored"]:
        github.set_ignored(token, thread_id)
    # action column stays 'done' (so the show_archived filter handles it);
    # the AI timeline gets 'muted' to preserve the stronger-than-Done signal.
    _apply_action(thread_id, "done", log_action="muted", unread=0, ignored=1)
    return _archive_response(thread_id)


# Fixed durations offered by the picker dropdown (the route accepts any
# 1..366, so the AI's `snooze_days` estimate can be posted as a shortcut).
SNOOZE_PICKER_DAYS = (1, 3, 7, 14, 30, 60, 180, 365)


@app.post("/set/<thread_id>/snooze")
def set_snooze(thread_id: str):
    """Snooze a thread: archive it on GitHub (like Done) and set a wake
    timer; the poll loop resurfaces it (unread again) when the timer passes.
    POST `?days=N` (1..366) to snooze. Plain snooze stays subscribed, so new
    GitHub activity can resurface it before the timer. Add `&quiet=1` to
    *also* unsubscribe (`ignored=1`) for the duration — for a busy thread
    you want a periodic digest of, not a live feed — in which case nothing
    resurfaces it early and the wake re-subscribes. POST with no `days` (the
    "wake now" click on a snoozed row) clears it, re-subscribing if it was
    a quiet snooze."""
    token = app.config["GITHUB_TOKEN"]
    n = _load_one(thread_id)
    if not n:
        return ("", 404)
    days_raw = (request.values.get("days") or "").strip()
    if not days_raw:
        # Wake now — un-snooze, bring it back unread (the snooze cleared
        # unread when it fired; restore it the way a timer wake would), and
        # re-subscribe if this was a quiet snooze.
        if n["action"] == "snoozed" and n["ignored"]:
            github.set_subscribed(token, thread_id)
        _apply_action(thread_id, "unsnoozed", unread=1, ignored=0)
        return _render_row(_load_one(thread_id))
    try:
        days = int(days_raw)
    except ValueError:
        return ("", 400)
    if not 1 <= days <= 366:
        return ("", 400)
    quiet = (request.values.get("quiet") or "").strip().lower() in ("1", "true", "on")
    # Mirror Done's GitHub side-effect: archive unless already archived as
    # done/snoozed. Then sync the subscription to the snooze flavour — quiet
    # unsubscribes (like Mute), plain stays/returns subscribed.
    if n["action"] not in ("done", "snoozed"):
        github.mark_done(token, thread_id)
    if quiet and not n["ignored"]:
        github.set_ignored(token, thread_id)
    elif not quiet and n["ignored"]:
        github.set_subscribed(token, thread_id)
    until = int(time.time()) + days * 86400
    payload = {"until": until}
    if quiet:
        payload["quiet"] = True
    _apply_action(
        thread_id, "snoozed", unread=0, ignored=(1 if quiet else 0),
        snooze_until=until, event_payload=payload,
    )
    return _archive_response(thread_id)


def _log_priority_change(
    conn: sqlite3.Connection,
    thread_id: str,
    from_score: float | None,
    to_score: float | None,
) -> None:
    """Append a `priority_change` event (payload {from, to} — 0..1 floats, or
    null = "auto"), coalescing with an immediately-prior one: if the latest
    event on this thread is already a `priority_change` (no GitHub / AI /
    other activity in between), update it in place rather than stacking — and
    if the run round-trips back to its starting point, drop it entirely.
    Keeps `from` pinned to the value before the first change of the run."""
    if from_score == to_score:
        return
    now = int(time.time())
    last = conn.execute(
        "SELECT id, kind, payload_json FROM thread_events "
        "WHERE thread_id = ? ORDER BY ts DESC, id DESC LIMIT 1",
        (thread_id,),
    ).fetchone()
    if last and last["kind"] == "priority_change":
        try:
            prev = json.loads(last["payload_json"])
        except (ValueError, TypeError):
            prev = {}
        origin = prev.get("from")
        if to_score == origin:
            conn.execute("DELETE FROM thread_events WHERE id = ?", (last["id"],))
            return
        conn.execute(
            "UPDATE thread_events SET payload_json = ?, ts = ? WHERE id = ?",
            (json.dumps({"from": origin, "to": to_score}, ensure_ascii=False),
             now, last["id"]),
        )
        return
    db.write_thread_event(
        conn,
        thread_id=thread_id,
        ts=now,
        kind="priority_change",
        source="user",
        payload={"from": from_score, "to": to_score},
    )


@app.post("/set/<thread_id>/priority")
def set_priority(thread_id: str):
    """Pin the thread's priority. The picker posts a band name (one of
    PRIORITY_LEVELS); we store the band's representative 0..1 score. Posting
    the band the current pin already sits in clears it back to "auto" (NULL →
    fall back to the AI verdict's score) — toggle-to-deselect, like the
    dismissal buttons. Logs a coalesced `priority_change` timeline event so
    the next judgment reads it as calibration; doesn't invalidate the verdict
    (a manual priority tweak isn't new context)."""
    n = _load_one(thread_id)
    if not n:
        return ("", 404)
    requested = (request.values.get("level") or "").strip()
    if requested not in PRIORITY_LEVELS:
        return ("", 400)
    current = n["priority_user"]  # 0..1 float or None (clamped in _row_to_dict)
    current_band = _score_to_priority_level(current) if current is not None else None
    new_score = None if requested == current_band else _PRIORITY_LEVEL_SCORE[requested]
    conn = db.connect()
    try:
        conn.execute(
            "UPDATE notifications SET priority_user = ? WHERE id = ?",
            (new_score, thread_id),
        )
        _log_priority_change(conn, thread_id, current, new_score)
    finally:
        conn.close()
    return _render_row(_load_one(thread_id))


@app.post("/toggle/<thread_id>/track")
def toggle_track(thread_id: str):
    """Toggle item-level tracked state. Persists locally; no GitHub API call."""
    n = _load_one(thread_id)
    if not n:
        return ("", 404)
    new_val = 0 if n["is_tracked"] else 1
    conn = db.connect()
    try:
        conn.execute(
            "UPDATE notifications SET is_tracked = ? WHERE id = ?",
            (new_val, thread_id),
        )
    finally:
        conn.close()
    return _render_row(_load_one(thread_id))


@app.post("/set/<thread_id>/mute-kinds")
def set_mute_kinds(thread_id: str):
    """Toggle one notification kind in the thread's per-thread mute set
    (muted_kinds JSON array). When a poll re-delivers this thread with new
    activity entirely of muted kinds, github._apply_mute_filter absorbs it —
    re-applies the prior row state, folds the activity into the baselines, and
    leaves the sort position frozen. Persists locally; no GitHub API call."""
    kind = (request.values.get("kind") or "").strip()
    if kind not in MUTE_KINDS:
        return ("", 400)
    n = _load_one(thread_id)
    if not n:
        return ("", 404)
    if kind not in _MUTE_KINDS_BY_TYPE.get(n["type"], ()):
        return ("", 400)  # not an applicable kind for this notification type
    current = set(n.get("muted_kinds") or [])
    current.symmetric_difference_update({kind})
    _write_muted_kinds(thread_id, current)
    return _render_row(_load_one(thread_id))


def _write_muted_kinds(thread_id: str, kinds) -> None:
    """Persist a thread's muted_kinds set (NULL when empty), MUTE_KINDS order."""
    stored = [k for k in MUTE_KINDS if k in set(kinds)]
    conn = db.connect()
    try:
        conn.execute(
            "UPDATE notifications SET muted_kinds = ? WHERE id = ?",
            (json.dumps(stored) if stored else None, thread_id),
        )
    finally:
        conn.close()


@app.post("/ai/<thread_id>/apply-mute-suggestion")
def apply_mute_suggestion(thread_id: str):
    """Apply the cached verdict's whole subscription suggestion in one click —
    mute the suggested-mute kinds, unmute the suggested-unmute ones. Idempotent
    (re-applying when nothing's left to do is a no-op). User-initiated, like
    the per-kind toggles; no GitHub call. 404 if the thread's gone, 400 if
    there's no verdict to apply."""
    n = _load_one(thread_id)
    if not n:
        return ("", 404)
    av = n.get("ai_verdict")
    if not av or not av.get("has_subscription_suggestion"):
        return ("", 400)
    applicable = {k for k, _, mutable in _mute_kind_options(n["type"]) if mutable}
    muted = set(n.get("muted_kinds") or [])
    muted |= {k for k in av.get("mute_suggested", []) if k in applicable}
    muted -= set(av.get("unmute_suggested", []))
    _write_muted_kinds(thread_id, muted)
    return _render_row(_load_one(thread_id))


def _toggle_entity_track(table: str, key_col: str, key: str) -> bool:
    """Generic upsert-and-toggle for the people / repos / orgs tracked flag.
    Returns the new is_tracked value."""
    conn = db.connect()
    try:
        existing = conn.execute(
            f"SELECT is_tracked FROM {table} WHERE {key_col} = ?", (key,)
        ).fetchone()
        if existing:
            new_val = 0 if existing["is_tracked"] else 1
            conn.execute(
                f"UPDATE {table} SET is_tracked = ? WHERE {key_col} = ?",
                (new_val, key),
            )
        else:
            new_val = 1
            conn.execute(
                f"INSERT INTO {table} ({key_col}, is_tracked, last_seen_at) "
                "VALUES (?, 1, ?)",
                (key, int(time.time())),
            )
    finally:
        conn.close()
    return bool(new_val)


def _entity_track_response(kind: str, key: str, is_tracked: bool):
    """Build the HTMX response shared by all three track endpoints:
    re-render the originating row (so its server-side state is fresh),
    plus HX-Trigger 'entityTrackedChanged' so the JS listener can update
    matching trigger tints and the row stripe on every other affected row."""
    thread_id = request.values.get("thread_id")
    body = ""
    if thread_id and (n := _load_one(thread_id)):
        body = _render_row(n)
    response = make_response(body, 200)
    response.headers["HX-Trigger"] = json.dumps({
        "entityTrackedChanged": {
            "kind": kind, "key": key, "is_tracked": is_tracked,
        }
    })
    return response


@app.post("/people/<login>/track")
def toggle_person_track(login: str):
    """Toggle person-level tracked state. Persists locally; no GitHub API call."""
    new_val = _toggle_entity_track("people", "login", login)
    return _entity_track_response("person", login, new_val)


@app.post("/repos/<owner>/<name>/track")
def toggle_repo_track(owner: str, name: str):
    """Toggle repo-level tracked state. The repo key is 'owner/name' to match
    notifications.repo's stored format."""
    repo = f"{owner}/{name}"
    new_val = _toggle_entity_track("repos", "name", repo)
    return _entity_track_response("repo", repo, new_val)


@app.post("/orgs/<owner>/track")
def toggle_org_track(owner: str):
    """Toggle org-level tracked state (the owner half of owner/repo)."""
    new_val = _toggle_entity_track("orgs", "name", owner)
    return _entity_track_response("org", owner, new_val)


def _save_entity_note(table: str, key_col: str, key: str, note: str | None) -> bool:
    """Upsert note_user on people / repos / orgs. Allows attaching a note to
    an entity that's never been tracked (the row is created on first save).
    Returns the new has-note flag."""
    conn = db.connect()
    try:
        conn.execute(
            f"INSERT INTO {table} ({key_col}, note_user, last_seen_at) "
            "VALUES (?, ?, ?) "
            f"ON CONFLICT({key_col}) DO UPDATE SET note_user = excluded.note_user",
            (key, note, int(time.time())),
        )
    finally:
        conn.close()
    return bool(note)


def _entity_note_response(kind: str, key: str, has_note: bool):
    """Silent 204 + HX-Trigger 'entityNoteChanged' so the JS listener can
    flip the has-note styling on every matching pencil across rows."""
    response = make_response("", 204)
    response.headers["HX-Trigger"] = json.dumps({
        "entityNoteChanged": {"kind": kind, "key": key, "has_note": has_note}
    })
    return response


def _form_note() -> str | None:
    return request.form.get("note_user", "").strip() or None


@app.post("/note/<thread_id>")
def save_note(thread_id: str):
    """Save user note for a notification. Silent (no swap); HTMX fires this on
    textarea change with a small delay. Broadcasts entityNoteChanged so the
    pencil's has-note styling updates without a row swap."""
    note = _form_note()
    conn = db.connect()
    try:
        conn.execute(
            "UPDATE notifications SET note_user = ? WHERE id = ?",
            (note, thread_id),
        )
    finally:
        conn.close()
    return _entity_note_response("item", thread_id, bool(note))


@app.post("/people/<login>/note")
def save_person_note(login: str):
    has_note = _save_entity_note("people", "login", login, _form_note())
    return _entity_note_response("person", login, has_note)


@app.post("/repos/<owner>/<name>/note")
def save_repo_note(owner: str, name: str):
    repo = f"{owner}/{name}"
    has_note = _save_entity_note("repos", "name", repo, _form_note())
    return _entity_note_response("repo", repo, has_note)


@app.post("/orgs/<owner>/note")
def save_org_note(owner: str):
    has_note = _save_entity_note("orgs", "name", owner, _form_note())
    return _entity_note_response("org", owner, has_note)


def _ai_response(thread_id: str, error: str | None):
    """Re-render the row, optionally attaching HX-Trigger 'showError' so the
    status dot flashes red and a toast surfaces the failure. Shape mirrors
    the existing _entity_track_response / _table_response helpers."""
    n = _load_one(thread_id)
    if not n:
        return ("", 404)
    body = _render_row(n)
    response = make_response(body, 200)
    if error:
        response.headers["HX-Trigger"] = json.dumps({"showError": {"message": error}})
    return response


@app.post("/ai/<thread_id>/judge")
def ai_judge(thread_id: str):
    """Generate a verdict for one thread. The verdict is cached on the row
    but no GitHub or DB state mutates until the user clicks Approve.

    Three invocation modes, derived from the request shape:
      - body present                          → 'chat'         (the user
        sent a message — saved as a user_chat event first — and wants a
        fresh verdict; the AI may answer in the verdict's optional `reply`
        field, but omits it when there's nothing to say back)
      - body empty + cached verdict exists    → 're_evaluate'  (Re-ask
        button; focus on what's changed since the last verdict)
      - body empty + no cached verdict        → 'summary'      (first
        Ask AI on this row; standard summarize-and-propose)
    The mode is passed through to ai.judge → user message → system
    prompt, which branches the description tone accordingly."""
    body = (request.form.get("body") or "").strip()
    error: str | None = None
    conn = db.connect()
    try:
        if body:
            mode = "chat"
            db.write_thread_event(
                conn,
                thread_id=thread_id,
                ts=int(time.time()),
                kind="user_chat",
                source="user",
                payload={"body": body},
            )
        else:
            row = conn.execute(
                "SELECT ai_verdict_at FROM notifications WHERE id = ?",
                (thread_id,),
            ).fetchone()
            mode = "re_evaluate" if (row and row["ai_verdict_at"]) else "summary"
        try:
            ai.judge(
                thread_id,
                conn,
                user_login=app.config.get("USER_LOGIN"),
                user_teams=app.config.get("USER_TEAMS"),
                invocation_mode=mode,
            )
        except ai.AIError as e:
            log.warning("AI judge failed for %s: %s", thread_id, e)
            error = f"AI judge failed: {e}"
        except Exception as e:  # noqa: BLE001 — surface unexpected failures
            log.exception("AI judge crashed for %s", thread_id)
            error = f"AI judge crashed: {e}"
    finally:
        conn.close()
    return _ai_response(thread_id, error)


@app.post("/ai/<thread_id>/chat")
def ai_chat(thread_id: str):
    """Persist a free-text user message as a user_chat thread_event,
    return the rendered timeline-event LI so HTMX can append it to the
    open popover's <ol class="timeline-list"> via hx-swap=beforeend.
    The composer's textarea is cleared client-side after a successful
    POST so subsequent messages append rather than re-save the same draft.
    Empty bodies short-circuit with 204 — nothing to append."""
    body = (request.form.get("body") or "").strip()
    if not body:
        return ("", 204)
    ts = int(time.time())
    conn = db.connect()
    try:
        db.write_thread_event(
            conn,
            thread_id=thread_id,
            ts=ts,
            kind="user_chat",
            source="user",
            payload={"body": body},
        )
        repo_row = conn.execute(
            "SELECT repo FROM notifications WHERE id = ?", (thread_id,)
        ).fetchone()
    finally:
        conn.close()
    # Render via the same partial the row template uses, so styling and
    # markup stay in one place. user_login isn't needed for user_chat
    # events (no author badge applies; the actor is always "You"). cur_repo
    # + tracked-people context feed ghmd's inline #ref / @mention rendering.
    ev = _format_event_for_render(
        {"ts": ts, "kind": "user_chat", "source": "user",
         "payload_json": json.dumps({"body": body})},
        ts,
        cur_repo=(repo_row["repo"] if repo_row else None),
        tracked_people=_tracked_people(),
        notes_people=_entity_notes("people", "login"),
    )
    return render_template("_timeline_event.html", ev=ev, thread_id=thread_id)
