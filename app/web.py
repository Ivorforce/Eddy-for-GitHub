"""Flask app + routes."""
from __future__ import annotations

import json
import logging
import math
import time
from datetime import datetime, timezone

from flask import Flask, make_response, render_template, request

from . import ai, db, github

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
    return f"{secs // 86400}d ago"


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
    "unread, ignored, action, action_source, "
    "details_json, details_fetched_at, "
    "seen_reasons, baseline_comments, "
    "pr_reactions_json, unique_commenters, unique_reviewers, "
    "pr_review_state, baseline_review_state, "
    "note_user, note_ai, is_tracked, "
    "ai_verdict_json, ai_verdict_at, ai_verdict_model"
)


# Human-readable labels for the action_now and set_tracked enums in the
# AI verdict pill. Mirrors the JSON enum values defined in app/ai.py:TOOL_DEF.
# 'none' is intentionally absent — when the AI proposes no state change,
# the priority is the verdict (see _AI_NONE_BY_PRIORITY) and the pill
# carries that word instead. For the other actions, the action itself
# is the user-facing instruction ("mark read" already implies "noise"),
# so priority is conveyed only via the pill's color tint.
_AI_ACTION_LABELS = {
    "mark_read": "mark read",
    "mute":      "mute",
    "archive":   "archive",
}
# When action_now == "none", priority bucket drives the headline word.
_AI_NONE_BY_BUCKET = {
    "low":    "ignore",
    "normal": "when convenient",
    "high":   "important",
}
_AI_TRACK_LABELS = {
    "track":   "track",
    "untrack": "untrack",
    # 'leave' intentionally absent — pill omits it.
}

# Sentence-case verbs for the Approve button's tooltip — we want the user
# to know exactly what clicking ✓ will do, not the generic "apply all
# proposed actions". When action_now=='none' AND set_tracked=='leave',
# there's nothing to apply and the button is disabled.
_AI_APPROVE_ACTION_VERBS = {
    "mark_read": "Mark read",
    "mute":      "Mute",
    "archive":   "Archive",
}
_AI_APPROVE_TRACK_VERBS = {
    "track":   "track",
    "untrack": "untrack",
}


def _approve_label(action_now: str, set_tracked: str, row: dict) -> str | None:
    """Build the Approve-button tooltip from the verdict's action/track
    fields, accounting for what's already true on the row. Returns None
    when every proposed change is already in effect (so the click would
    be a no-op) — caller should disable the button.

    Rules:
      mark_read on a read+subscribed row → drop (already read)
      mute      on an already-muted row  → drop
      archive   on a done row            → drop
      track     on an already-tracked    → drop
      untrack   on an already-untracked  → drop
    """
    is_unread  = bool(row.get("unread"))
    is_ignored = bool(row.get("ignored"))
    is_done    = (row.get("action") == "done")
    is_tracked = bool(row.get("is_tracked"))

    action_part: str | None = None
    if action_now == "mark_read" and (is_unread or is_ignored):
        action_part = _AI_APPROVE_ACTION_VERBS["mark_read"]
    elif action_now == "mute" and not is_ignored:
        action_part = _AI_APPROVE_ACTION_VERBS["mute"]
    elif action_now == "archive" and not is_done:
        action_part = _AI_APPROVE_ACTION_VERBS["archive"]

    track_part: str | None = None
    if set_tracked == "track" and not is_tracked:
        track_part = _AI_APPROVE_TRACK_VERBS["track"]
    elif set_tracked == "untrack" and is_tracked:
        track_part = _AI_APPROVE_TRACK_VERBS["untrack"]

    if action_part and track_part:
        return f"{action_part} and {track_part}"
    if action_part:
        return action_part
    if track_part:
        return track_part.capitalize()
    return None


def _priority_bucket(score: float) -> str:
    """Map a 0.0-1.0 priority score to one of three buckets — drives the
    pill's color class. Boundaries chosen so the three named anchors
    ('ignore' at 0.1, 'when convenient' at 0.5, 'important' at 0.7) all
    fall in the bucket their word suggests. Sort order uses the float
    directly for finer-grained ranking."""
    if score < 0.34:
        return "low"
    if score < 0.67:
        return "normal"
    return "high"


# Vocabulary of "relevant signals" the AI can flag, mapped to display
# (label, css_class). The keys mirror app/ai.py:SIGNAL_VOCAB; adding one
# requires touching both files. CSS classes reuse the existing .status-pill
# variants where they exist (so colors stay consistent with the rule-based
# Manual mode), with .signal-neutral as a quiet fallback for informational
# signals that don't have a strong color.
_SIGNAL_LABELS: dict[str, tuple[str, str]] = {
    "review_you":         ("Review you",        "action-review-you"),
    "review_team":        ("Review team",       "action-review-team"),
    "assigned":           ("Assigned",          "action-assigned"),
    "mentioned":          ("Mentioned",         "flag-mention"),
    "approved":           ("Approved",          "review-approved"),
    "changes_requested":  ("Changes requested", "review-changes"),
    "merge_dirty":        ("Conflicts",         "sev-danger"),
    "merge_unstable":     ("CI failing",        "sev-warning"),
    "merge_behind":       ("Behind base",       "sev-warning"),
    "new_comments":       ("New comments",      "new-comments"),
    "popular":            ("Popular",           "signal-positive"),
    "controversial":      ("Controversial",     "signal-warning"),
    "engaged":            ("Engaged",           "signal-neutral"),
    "merged":             ("Merged",            "signal-neutral"),
    "closed":             ("Closed",            "signal-neutral"),
    "answered":           ("Answered",          "signal-positive"),
    "draft":              ("Draft",             "signal-neutral"),
    "tracked_author":     ("Tracked author",    "signal-tracked"),
    "tracked_repo":       ("Tracked repo",      "signal-tracked"),
    "tracked_org":        ("Tracked org",       "signal-tracked"),
    "bot_author":         ("Bot",               "signal-neutral"),
    "first_timer":        ("First-time",        "signal-neutral"),
    "large_diff":         ("Large diff",        "signal-neutral"),
    "small_diff":         ("Small diff",        "signal-neutral"),
}

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
    "Issue": "Issue",
    "Discussion": "Disc",
    "Release": "Rel",
    "CheckSuite": "Check",
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
# Hide done filter. Distinct from "closed" in any single GitHub sense:
# PRs report 'merged' or 'closed_pr', Issues report 'closed_completed'
# or 'closed_not_planned', Discussions report 'answered' or 'closed'.
# All count as done for triage purposes — nothing left to do.
DONE_TYPE_STATES = {
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
# applies, so the row always shows *why* it's here. Always rendered as prose.
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


def _status_summary(d: dict) -> tuple[dict | None, str]:
    """Pick the most action-defining signal as the headline pill, demote the
    rest to a ' · '-joined prose subhead. Empty pill + empty prose means
    nothing to flag — the cell renders blank.

    Priority is action-defining over merely-blocking: a "Review you" headline
    tells you what to do; "Conflicts" only tells you something is broken (and
    still shows up in the prose). Neutral candidates (purely informational,
    e.g. "+N comments") never get promoted to the pill — they only render as
    prose, even when they're the highest-priority candidate. When no candidate
    fires at all, fall back to the GitHub `reason` so the user can still see
    why the row is here. Designed so a future AI verdict can replace the
    rules-based pill/prose without changing the template."""
    # (rank, css_class, text, neutral)
    candidates: list[tuple[int, str, str, bool]] = []

    merge = d.get("merge_state")
    if merge:
        if merge[1] == "danger":
            candidates.append((3, "sev-danger", merge[0], False))
        else:
            candidates.append((5, "sev-warning", merge[0], False))

    action = d.get("action_needed")
    if action:
        candidates.append(
            (1, f"action-{action.replace('_', '-')}", _ACTION_LABELS[action], False)
        )

    if d.get("mentioned_since"):
        candidates.append((2, "flag-mention", "Mentioned", False))

    rs = d.get("pr_review_state")
    if rs in _REVIEW_LABELS:
        rs_class = "review-approved" if rs == "approved" else "review-changes"
        candidates.append((4, rs_class, _REVIEW_LABELS[rs], False))

    interest = (d.get("meta") or {}).get("interest") or {}
    new_c = interest.get("new_comments") or 0
    if new_c:
        candidates.append(
            (6, "new-comments", f"+{new_c} comment{'' if new_c == 1 else 's'}", True)
        )

    if not candidates:
        fallback = _REASON_FALLBACK_LABELS.get(d.get("reason") or "")
        return None, fallback or ""

    candidates.sort(key=lambda c: c[0])
    rank, head_class, head_text, head_neutral = candidates[0]
    if head_neutral:
        prose = " · ".join(text for _, _, text, _ in candidates)
        return None, prose
    pill = {"text": head_text, "cls": head_class}
    prose = " · ".join(text for _, _, text, _ in candidates[1:])
    return pill, prose


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


# Display labels for user_action events in the popover timeline. Source
# of truth for action strings is web._apply_action and ai._set_state /
# apply_verdict / dismiss_verdict; keep this in sync if either grows new
# action labels. Unknown actions render as the raw key (forward-compat).
_USER_ACTION_LABELS = {
    "read":             "Marked read",
    "muted":            "Muted",
    "done":             "Archived",
    "kept_unread":      "Kept unread",
    "unmuted":          "Unmuted",
    "approve_verdict":  "Approved verdict",
    "dismiss_verdict":  "Dismissed verdict",
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
}


def _verdict_render_dict(payload: dict) -> dict:
    """Display-ready bits of a past ai_verdict event's payload, for
    rendering inside the timeline list. Distinct from _ai_verdict_dict,
    which shapes the *cached pending* verdict (and includes approve_label
    / stale logic that don't apply to historical entries)."""
    score_raw = payload.get("priority_score")
    if isinstance(score_raw, (int, float)):
        priority_score = max(0.0, min(1.0, float(score_raw)))
    else:
        priority_score = 0.5
    priority_bucket = _priority_bucket(priority_score)

    raw_signals = payload.get("relevant_signals") or []
    signals: list[dict] = []
    if isinstance(raw_signals, list):
        for key in raw_signals[:3]:
            if isinstance(key, str) and key in _SIGNAL_LABELS:
                label, cls = _SIGNAL_LABELS[key]
                signals.append({"key": key, "label": label, "cls": cls})

    return {
        "description":    (payload.get("description") or "").strip(),
        "priority":       priority_bucket,
        "priority_score": priority_score,
        "signals":        signals,
        "model":          payload.get("model") or "",
    }


# Comments closer than this collapse into a single timeline line
# ("5 comments by alice, bob"). Any larger gap reads as a separate
# conversation worth its own entry — picked at ~1 month so a single
# discussion (typically minutes-to-days of activity) stays grouped while
# distinct flare-ups months apart don't merge into a misleading summary.
_COMMENT_COALESCE_GAP_SECS = 30 * 86400


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


def _format_event_for_render(row, now: int, user_login: str | None = None) -> dict:
    """Convert one thread_events row into a display-ready dict for the
    popover timeline. Each kind gets an `actor` (display name shown in
    the row gutter) and either a `summary` string (one-liner kinds) or
    full payload fields (ai_verdict / user_chat) for the chat-bubble
    rendering. comment / review events also pick up `author_badge_class`
    so the template can render the same icon styling the Repo column
    uses (self/member/first-time/generic)."""
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
        out["actor"] = "AI" if source == "ai" else "You"
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
        out["verdict"] = _verdict_render_dict(payload)
    elif kind == "user_chat":
        out["actor"] = "You"
        # body rendered as-is by the template
    return out


def _attach_timeline(d: dict, conn: sqlite3.Connection) -> None:
    """Mutate `d` in place: attach `timeline` + `events_since_verdict`
    fields when the row has a cached verdict (i.e., the popover will
    render). Skipped otherwise so bulk list rendering doesn't pay the
    cost on rows that aren't going to display a popover.

    The latest `ai_verdict` event in the timeline gets `is_pending=True`
    and inherits the cached verdict's `approve_label`. The popover
    template renders the in-bubble Approve / Dismiss buttons only on
    that event — past verdicts are history; you can't approve them
    retroactively."""
    verdict = d.get("ai_verdict")
    if not verdict:
        return
    rows = conn.execute(
        """
        SELECT ts, kind, source, payload_json
          FROM thread_events
         WHERE thread_id = ?
         ORDER BY ts ASC, id ASC
        """,
        (d["id"],),
    ).fetchall()
    now = int(time.time())
    user_login = app.config.get("USER_LOGIN")
    timeline = [_format_event_for_render(r, now, user_login=user_login) for r in rows]
    # Coalesce comment runs BEFORE marking the pending ai_verdict —
    # ai_verdict events aren't touched by coalescing, so the in-bubble
    # approve/dismiss markers still land on the same object identity.
    timeline = _coalesce_comments(timeline)
    # Walk from newest backward; first ai_verdict wins.
    for ev in reversed(timeline):
        if ev["kind"] == "ai_verdict":
            ev["is_pending"] = True
            ev["approve_label"] = verdict.get("approve_label")
            break
    d["timeline"] = timeline
    # "Stale" count: GitHub events + the user's own typed messages that
    # postdate the cached verdict. user_action events on the verdict
    # itself (approve/dismiss) clear the cache, so they never appear
    # here; other user_actions (mark-read on the row directly) don't
    # really "invalidate" a verdict, so they're excluded.
    after = verdict.get("at") or 0
    d["events_since_verdict"] = sum(
        1 for r in rows
        if r["ts"] > after and r["kind"] in ("comment", "review", "user_chat")
    )


def _ai_verdict_dict(
    verdict_json: str | None, at: int | None, model: str | None,
    details_fetched_at: int | None,
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
    action_now = verdict.get("action_now") or "none"
    set_tracked = verdict.get("set_tracked") or "leave"

    # priority_score is the canonical priority signal (0.0-1.0 float).
    # Legacy verdicts cached before the schema change carry the old
    # 'priority' enum (low/normal/high) — map it to a representative float
    # so old verdicts still render and sort sensibly.
    score_raw = verdict.get("priority_score")
    if isinstance(score_raw, (int, float)):
        priority_score = max(0.0, min(1.0, float(score_raw)))
    else:
        # Legacy fallback: low → 0.2, normal → 0.5, high → 0.8.
        legacy_priority = verdict.get("priority")
        priority_score = {"low": 0.2, "normal": 0.5, "high": 0.8}.get(
            legacy_priority, 0.5
        )
    priority_bucket = _priority_bucket(priority_score)

    parts: list[str] = []
    if action_now == "none":
        # No state change proposed — priority bucket becomes the verdict
        # word so the pill answers "what do I do?" instead of "what's
        # changing?".
        parts.append(_AI_NONE_BY_BUCKET.get(priority_bucket, "review"))
    else:
        parts.append(_AI_ACTION_LABELS.get(action_now, action_now))
    if set_tracked in _AI_TRACK_LABELS:
        parts.append(_AI_TRACK_LABELS[set_tracked])
    pill_text = " · ".join(parts)

    age_text = _humanize_age(int(time.time()) - at)

    # description is the single-field replacement for summary+rationale.
    # Legacy verdicts (cached before the schema change) still have the old
    # split; join them so the popover renders consistently regardless of
    # which shape the cached blob happens to use.
    description = (verdict.get("description") or "").strip()
    if not description and (verdict.get("summary") or verdict.get("rationale")):
        s = (verdict.get("summary") or "").strip()
        r = (verdict.get("rationale") or "").strip()
        description = " — ".join(p for p in (s, r) if p)

    # Render relevant_signals (priority-ordered enum keys) into displayable
    # (key, label, cls) triples. Drop unknown keys silently — they're
    # forward-compat with vocabulary expansions on a model that's been
    # told about a key the running app doesn't yet know.
    raw_signals = verdict.get("relevant_signals") or []
    signals = []
    if isinstance(raw_signals, list):
        for key in raw_signals[:3]:
            if isinstance(key, str) and key in _SIGNAL_LABELS:
                label, cls = _SIGNAL_LABELS[key]
                signals.append({"key": key, "label": label, "cls": cls})

    return {
        "verdict":         verdict,
        "action_now":      action_now,
        "set_tracked":     set_tracked,
        "priority_score":  priority_score,
        "priority":        priority_bucket,  # low/normal/high — drives CSS
        "description":     description,
        "signals":         signals,
        "model":           model or "",
        "at":              at,
        "age_text":        age_text,
        "pill_text":       pill_text,
        # approve_label is set by _row_to_dict — it depends on the row's
        # current state (already read? already tracked?) which this
        # function doesn't see.
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
    d["merge_state"] = _merge_state(details, d["type"])
    # 'New since last action': review state changed since the user last engaged
    # (action or first ingest, baseline NULL means "never engaged").
    baseline_rs = d.pop("baseline_review_state", None)
    d["is_review_new"] = bool(d["pr_review_state"]) and d["pr_review_state"] != baseline_rs
    d["status_pill"], d["status_prose"] = _status_summary(d)

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

    # Pending AI verdict (None if no judgment is queued for this row).
    d["ai_verdict"] = _ai_verdict_dict(
        ai_verdict_json, ai_verdict_at, ai_verdict_model, details_fetched_at,
    )
    # approve_label depends on row state (no-op detection), so it's set
    # here rather than inside _ai_verdict_dict.
    if d["ai_verdict"]:
        d["ai_verdict"]["approve_label"] = _approve_label(
            d["ai_verdict"]["action_now"],
            d["ai_verdict"]["set_tracked"],
            d,
        )

    return d


def _load_notifications():
    t_p = _tracked_people()
    t_r = _tracked_set("repos")
    t_o = _tracked_set("orgs")
    n_p = _entity_notes("people", "login")
    n_r = _entity_notes("repos", "name")
    n_o = _entity_notes("orgs", "name")
    conn = db.connect()
    try:
        rows = conn.execute(
            f"SELECT {_ROW_COLS} FROM notifications "
            "WHERE COALESCE(action, '') != 'done' "
            "ORDER BY updated_at DESC"
        ).fetchall()
        out = [_row_to_dict(r, t_p, t_r, t_o, n_p, n_r, n_o) for r in rows]
        # Timeline attached only on rows that will actually render the
        # popover (i.e., have a cached verdict). Single point query each;
        # bulk-list cost stays bounded because the verdict cache is rare.
        for d in out:
            _attach_timeline(d, conn)
        return out
    finally:
        conn.close()


def _load_repo_options() -> tuple[list[str], list[str]]:
    """Distinct owners and repo names from active (non-done) notifications.
    Drives the Owner and Repo filter dropdowns. Names are de-duplicated
    across owners — if 'godot' shows up under two owners it appears once."""
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT DISTINCT repo FROM notifications "
            "WHERE COALESCE(action, '') != 'done' AND repo != ''"
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
    mode = (src.get("triage_mode") or "manual").strip()
    if mode not in ("manual", "ai"):
        mode = "manual"
    return {
        "actions":      src.getlist("actions"),
        "hide_read":    bool(src.get("hide_read")),
        "hide_done":    bool(src.get("hide_done")),
        "tracked_only": bool(src.get("tracked_only")),
        "mine_only":    bool(src.get("mine_only")),
        "owner":        src.get("owner") or "",
        "repo":         src.get("repo") or "",
        "sort":         src.get("sort") or "updated",
        "q":            (src.get("q") or "").strip(),
        "types":        src.getlist("types"),
        "triage_mode":  mode,
    }


def _render_row(n: dict):
    """Wrap render_template('_row.html', ...) so every row swap carries the
    active triage_mode. Read from request.values so per-row HTMX swaps that
    include #filters automatically pick up the current mode; falls back to
    'manual' when the request didn't include it (e.g. a stale buttontag)."""
    mode = (request.values.get("triage_mode") or "manual").strip()
    if mode not in ("manual", "ai"):
        mode = "manual"
    return render_template("_row.html", n=n, triage_mode=mode)


def _filter_and_sort(rows: list[dict], f: dict) -> list[dict]:
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
    if f["hide_done"]:
        rows = [r for r in rows if r["type_state"] not in DONE_TYPE_STATES]
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
    if f["sort"] == "engaged":
        rows.sort(
            key=lambda r: (
                ((r.get("meta") or {}).get("interest") or {}).get("engaged") or 0
            ),
            reverse=True,
        )
    elif f["sort"] == "stale":
        # Oldest-updated first. Pairs with Hide done to surface forgotten
        # open work; on its own, surfaces both forgotten and long-resolved.
        rows.sort(key=lambda r: r["updated_at"] or "")
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
    # headers are honest about what they're grouping. Non-temporal sorts
    # (engagement) get no bucketing at all — the order doesn't carry a
    # date semantics, so headers would be misleading.
    sort = f["sort"]
    if sort in ("updated", "stale"):
        for r in rows:
            r["bucket"] = _bucket(r["updated_at"])
    elif sort in ("newest", "oldest"):
        for r in rows:
            r["bucket"] = _bucket(r["created_at"])
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
        _attach_timeline(d, conn)
        return d
    finally:
        conn.close()


@app.get("/")
def index():
    f = _filters_from_request()
    rows = _filter_and_sort(_load_notifications(), f)
    owners, repo_names = _load_repo_options()
    return render_template(
        "index.html",
        notifications=rows,
        owners=owners,
        repo_names=repo_names,
        filters=f,
        triage_mode=f["triage_mode"],
        type_labels=TYPE_LABELS_LONG,
        action_labels=ACTION_FILTER_LABELS,
    )


@app.get("/list")
def list_view():
    """Re-render the table with current filter/sort params (no polling)."""
    f = _filters_from_request()
    rows = _filter_and_sort(_load_notifications(), f)
    return render_template(
        "_table.html", notifications=rows, error=None, filters=f,
        triage_mode=f["triage_mode"],
    )


def _table_response(error: str | None) -> "Response":
    """Re-render the table with current filters; if an error happened, attach
    HX-Trigger 'showError' so the status dot flips red without disrupting the swap."""
    f = _filters_from_request()
    rows = _filter_and_sort(_load_notifications(), f)
    body = render_template(
        "_table.html", notifications=rows, filters=f, triage_mode=f["triage_mode"],
    )
    response = make_response(body, 200)
    if error:
        response.headers["HX-Trigger"] = json.dumps({"showError": {"message": error}})
    return response


@app.post("/refresh")
def refresh():
    token = app.config["GITHUB_TOKEN"]
    error: str | None = None
    # ?auto=1 marks browser-driven refreshes (visibilitychange handler) so
    # they ride the poll predicate and skip the unread fetch on a quiet
    # inbox. A user-clicked refresh has no flag and forces a full sync so
    # the click never feels like it missed something.
    force_full = not request.values.get("auto")
    conn = db.connect()
    try:
        try:
            github.poll_once(conn, token, force_full=force_full)
        except Exception as e:
            log.exception("on-demand refresh failed")
            error = f"Refresh failed: {e}"
    finally:
        conn.close()
    return _table_response(error)


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


def _apply_action(
    thread_id: str,
    action: str,
    source: str = "user",
    **state,
) -> None:
    """Record action + actioned_at + action_source, plus arbitrary state columns.

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
            payload={"action": action},
        )
    finally:
        conn.close()


@app.post("/set/<thread_id>/read")
def set_read(thread_id: str):
    """Radio-style: set the row to 'read' state, OR deselect (back to unread)
    if it's already read. Handles transitions from any of unread / read / muted."""
    token = app.config["GITHUB_TOKEN"]
    n = _load_one(thread_id)
    if not n:
        return ("", 404)
    is_read_now = n["unread"] == 0 and n["ignored"] == 0
    if is_read_now:
        # Click already-active Read → deselect → mark unread locally.
        # GitHub REST API has no 'mark unread' (only the web UI can, via
        # /notifications/beta/unmark with session+CSRF auth — not worth it).
        # action='kept_unread' tells the reconciler not to flip it back.
        _apply_action(thread_id, "kept_unread", unread=1, ignored=0)
    else:
        if n["unread"]:
            github.mark_read(token, thread_id)
        if n["ignored"]:
            github.set_subscribed(token, thread_id)
        _apply_action(thread_id, "read", unread=0, ignored=0)
    return _render_row(_load_one(thread_id))


@app.post("/set/<thread_id>/muted")
def set_muted(thread_id: str):
    """Radio-style: set the row to 'muted' state (read + ignored), OR deselect
    (back to plain 'read') if it's already muted."""
    token = app.config["GITHUB_TOKEN"]
    n = _load_one(thread_id)
    if not n:
        return ("", 404)
    if n["ignored"]:
        # Click already-active Muted → deselect → become 'read' (unmute, stay read).
        github.set_subscribed(token, thread_id)
        _apply_action(thread_id, "unmuted", ignored=0)
    else:
        if n["unread"]:
            github.mark_read(token, thread_id)
        github.set_ignored(token, thread_id)
        _apply_action(thread_id, "muted", unread=0, ignored=1)
    return _render_row(_load_one(thread_id))


@app.post("/action/<thread_id>/done")
def action_done(thread_id: str):
    """Archive a thread on GitHub. With ?unsubscribe=1, also stop future
    notifications from it (set_ignored) — the common workflow when you
    don't care about a watch-driven thread, separate from plain Done
    where future activity may legitimately resurface the thread."""
    token = app.config["GITHUB_TOKEN"]
    github.mark_done(token, thread_id)
    state: dict = {"unread": 0}
    if request.values.get("unsubscribe"):
        github.set_ignored(token, thread_id)
        state["ignored"] = 1
    _apply_action(thread_id, "done", **state)
    return ("", 200)


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

    Optional `body` form field — if non-empty, write a user_chat event
    for it BEFORE judging, so the AI sees the user's message in the
    timeline as part of this judgment. Powers the popover's
    "Send + Re-ask" flow (Ctrl+Enter)."""
    body = (request.form.get("body") or "").strip()
    error: str | None = None
    conn = db.connect()
    try:
        try:
            if body:
                db.write_thread_event(
                    conn,
                    thread_id=thread_id,
                    ts=int(time.time()),
                    kind="user_chat",
                    source="user",
                    payload={"body": body},
                )
            ai.judge(
                thread_id,
                conn,
                user_login=app.config.get("USER_LOGIN"),
                user_teams=app.config.get("USER_TEAMS"),
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


@app.post("/ai/<thread_id>/approve")
def ai_approve(thread_id: str):
    """Execute the cached verdict's proposed mutations (read/mute/archive,
    track toggle, AI summary into note_ai), then clear the verdict cache."""
    token = app.config["GITHUB_TOKEN"]
    error: str | None = None
    conn = db.connect()
    try:
        try:
            ai.apply_verdict(thread_id, conn, token)
        except ai.AIError as e:
            log.warning("AI approve failed for %s: %s", thread_id, e)
            error = f"AI approve failed: {e}"
        except Exception as e:  # noqa: BLE001
            log.exception("AI approve crashed for %s", thread_id)
            error = f"AI approve crashed: {e}"
    finally:
        conn.close()
    return _ai_response(thread_id, error)


@app.post("/ai/<thread_id>/dismiss")
def ai_dismiss(thread_id: str):
    """Discard the cached verdict without applying any mutations."""
    conn = db.connect()
    try:
        ai.dismiss_verdict(thread_id, conn)
    finally:
        conn.close()
    return _ai_response(thread_id, None)


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
    finally:
        conn.close()
    # Render via the same partial the row template uses, so styling and
    # markup stay in one place. user_login isn't needed for user_chat
    # events (no author badge applies; the actor is always "You").
    ev = _format_event_for_render(
        {"ts": ts, "kind": "user_chat", "source": "user",
         "payload_json": json.dumps({"body": body})},
        ts,
    )
    return render_template("_timeline_event.html", ev=ev, thread_id=thread_id)
