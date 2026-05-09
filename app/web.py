"""Flask app + routes."""
from __future__ import annotations

import json
import logging
import math
import time
from datetime import datetime, timezone

from flask import Flask, make_response, render_template, request

from . import db, github

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


def _age_pill(iso: str | None) -> dict | None:
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
    title = f"Created {days_int} day{'' if days_int == 1 else 's'} ago ({iso})"
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
    "id, repo, type, title, reason, html_url, updated_at, "
    "unread, ignored, action, details_json, seen_reasons, baseline_comments, "
    "pr_reactions_json, unique_commenters, unique_reviewers, "
    "pr_review_state, baseline_review_state, "
    "note_user, is_tracked"
)

# author_association -> (badge css class, display label).
# Only high-signal associations get a badge; CONTRIBUTOR/NONE etc. stay quiet.
_AUTHOR_BADGE = {
    "OWNER":                  ("member",     "owner"),
    "MEMBER":                 ("member",     "member"),
    "COLLABORATOR":           ("collab",     "collab"),
    "FIRST_TIMER":            ("first-time", "first-time"),
    "FIRST_TIME_CONTRIBUTOR": ("first-time", "first-time"),
}

# GitHub reaction emoji buckets. Same user can react with multiple positives;
# max() approximates a lower bound on distinct users in that sentiment bucket
# (sum overcounts; max never overcounts a single category).
_POSITIVE_REACTIONS = ("+1", "heart", "hooray", "rocket", "laugh")
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
    Each is None when not applicable so the pill hides."""
    out = {"complexity": None, "reception": None, "interest": None}
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

    # Reactions: PRs come from the separately-fetched issue-form endpoint;
    # Issues already have them embedded in details_json.
    rx_dict: dict | None = None
    if subject_type == "PullRequest" and pr_reactions_json:
        try:
            rx_dict = json.loads(pr_reactions_json)
        except (ValueError, TypeError):
            rx_dict = None
    elif subject_type in ("Issue", "Discussion"):
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


def _bucket(updated_at_iso: str | None) -> str:
    """Group notifications into time buckets based on local-calendar age."""
    if not updated_at_iso:
        return "Earlier"
    try:
        dt = datetime.fromisoformat(updated_at_iso.replace("Z", "+00:00")).astimezone()
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
_REVIEW_LABELS = {
    "approved": "Approved",
    "changes_requested": "Changes requested",
}


def _status_summary(d: dict) -> tuple[dict | None, str]:
    """Pick the most action-defining signal as the headline pill, demote the
    rest to a ' · '-joined prose subhead. Empty pill + empty prose means
    nothing to flag — the cell renders blank.

    Priority is action-defining over merely-blocking: a "Review you" headline
    tells you what to do; "Conflicts" only tells you something is broken (and
    still shows up in the prose). Designed so a future AI verdict can replace
    the rules-based pill/prose without changing the template."""
    candidates: list[tuple[int, str, str]] = []  # (rank, css_class, text)

    merge = d.get("merge_state")
    if merge:
        if merge[1] == "danger":
            candidates.append((3, "sev-danger", merge[0]))
        else:
            candidates.append((5, "sev-warning", merge[0]))

    action = d.get("action_needed")
    if action:
        candidates.append(
            (1, f"action-{action.replace('_', '-')}", _ACTION_LABELS[action])
        )

    if d.get("mentioned_since"):
        candidates.append((2, "flag-mention", "Mentioned"))

    rs = d.get("pr_review_state")
    if rs in _REVIEW_LABELS:
        rs_class = "review-approved" if rs == "approved" else "review-changes"
        candidates.append((4, rs_class, _REVIEW_LABELS[rs]))

    interest = (d.get("meta") or {}).get("interest") or {}
    new_c = interest.get("new_comments") or 0
    if new_c:
        candidates.append(
            (6, "new-comments", f"+{new_c} comment{'' if new_c == 1 else 's'}")
        )

    if not candidates:
        return None, ""

    candidates.sort(key=lambda c: c[0])
    _, head_class, head_text = candidates[0]
    pill = {"text": head_text, "cls": head_class}
    prose = " · ".join(text for _, _, text in candidates[1:])
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
    seen_reasons_json = d.pop("seen_reasons", None)
    baseline_comments = d.pop("baseline_comments", None)
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
    d["age"] = _age_pill(details.get("created_at")) if details else None
    d["popularity"] = _popularity_pill(d["meta"].get("reception"))
    all_labels = _extract_labels(details_json)
    d["labels_visible"] = all_labels[:3]
    d["labels_extra"] = all_labels[3:]
    d["type_label"] = TYPE_LABELS.get(d["type"], d["type"])
    d["type_label_long"] = TYPE_LABELS_LONG.get(d["type"], d["type"])
    d["type_state"] = _type_state(details, d["type"])
    d["bucket"] = _bucket(d["updated_at"])
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
        return [_row_to_dict(r, t_p, t_r, t_o, n_p, n_r, n_o) for r in rows]
    finally:
        conn.close()


def _load_repos() -> list[str]:
    conn = db.connect()
    try:
        return [
            r["repo"] for r in conn.execute(
                "SELECT DISTINCT repo FROM notifications "
                "WHERE COALESCE(action, '') != 'done' AND repo != '' "
                "ORDER BY repo"
            ).fetchall()
        ]
    finally:
        conn.close()


def _filters_from_request() -> dict:
    src = request.values  # union of query string + form fields
    return {
        "action_only":  bool(src.get("action_only")),
        "hide_read":    bool(src.get("hide_read")),
        "tracked_only": bool(src.get("tracked_only")),
        "repo":         src.get("repo") or "",
        "sort":         src.get("sort") or "updated",
    }


def _filter_and_sort(rows: list[dict], f: dict) -> list[dict]:
    if f["action_only"]:
        rows = [r for r in rows if r["action_needed"] or r["mentioned_since"]]
    if f["hide_read"]:
        rows = [r for r in rows if r["unread"]]
    if f["tracked_only"]:
        rows = [
            r for r in rows
            if r["is_tracked"]
            or r["author_is_tracked"]
            or r["repo_is_tracked"]
            or r["org_is_tracked"]
        ]
    if f["repo"]:
        rows = [r for r in rows if r["repo"] == f["repo"]]
    if f["sort"] == "engaged":
        rows.sort(
            key=lambda r: (
                ((r.get("meta") or {}).get("interest") or {}).get("engaged") or 0
            ),
            reverse=True,
        )
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
        return _row_to_dict(row, t_p, t_r, t_o, n_p, n_r, n_o) if row else None
    finally:
        conn.close()


@app.get("/")
def index():
    f = _filters_from_request()
    rows = _filter_and_sort(_load_notifications(), f)
    return render_template(
        "index.html", notifications=rows, repos=_load_repos(), filters=f
    )


@app.get("/list")
def list_view():
    """Re-render the table with current filter/sort params (no polling)."""
    rows = _filter_and_sort(_load_notifications(), _filters_from_request())
    return render_template("_table.html", notifications=rows, error=None)


def _table_response(error: str | None) -> "Response":
    """Re-render the table with current filters; if an error happened, attach
    HX-Trigger 'showError' so the status dot flips red without disrupting the swap."""
    rows = _filter_and_sort(_load_notifications(), _filters_from_request())
    body = render_template("_table.html", notifications=rows)
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
    cols = {
        "action": action,
        "actioned_at": int(time.time()),
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
    return render_template("_row.html", n=_load_one(thread_id))


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
    return render_template("_row.html", n=_load_one(thread_id))


@app.post("/action/<thread_id>/done")
def action_done(thread_id: str):
    github.mark_done(app.config["GITHUB_TOKEN"], thread_id)
    _apply_action(thread_id, "done", unread=0)
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
    return render_template("_row.html", n=_load_one(thread_id))


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
        body = render_template("_row.html", n=n)
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
