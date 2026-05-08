"""Flask app + routes."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from flask import Flask, render_template, request

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


_ROW_COLS = (
    "id, repo, type, title, reason, html_url, updated_at, "
    "unread, ignored, action, details_json, seen_reasons, baseline_comments, "
    "pr_reactions_json, unique_commenters, pr_review_state, baseline_review_state"
)

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
    elif subject_type == "Issue":
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
    # Engagement = max-aggregated reactions + unique commenter count.
    engaged = pos + neg + eyes + (unique_commenters or 0)
    if comments > 0 or engaged > 0:
        out["interest"] = {
            "comments": comments,
            "new_comments": new_comments,
            "engaged": engaged,
            "commenters": unique_commenters,
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


def _row_to_dict(row) -> dict:
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
    )
    all_labels = _extract_labels(details_json)
    d["labels_visible"] = all_labels[:3]
    d["labels_extra"] = all_labels[3:]
    d["type_label"] = TYPE_LABELS.get(d["type"], d["type"])
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
    return d


def _load_notifications():
    conn = db.connect()
    try:
        rows = conn.execute(
            f"SELECT {_ROW_COLS} FROM notifications "
            "WHERE COALESCE(action, '') != 'done' "
            "ORDER BY updated_at DESC"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
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
        "action_only": bool(src.get("action_only")),
        "hide_read":   bool(src.get("hide_read")),
        "repo":        src.get("repo") or "",
        "sort":        src.get("sort") or "updated",
    }


def _filter_and_sort(rows: list[dict], f: dict) -> list[dict]:
    if f["action_only"]:
        rows = [r for r in rows if r["action_needed"] or r["mentioned_since"]]
    if f["hide_read"]:
        rows = [r for r in rows if r["unread"]]
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
    conn = db.connect()
    try:
        row = conn.execute(
            f"SELECT {_ROW_COLS} FROM notifications WHERE id = ?",
            (thread_id,),
        ).fetchone()
        return _row_to_dict(row) if row else None
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


@app.post("/refresh")
def refresh():
    token = app.config["GITHUB_TOKEN"]
    error: str | None = None
    conn = db.connect()
    try:
        try:
            github.poll_once(conn, token)
        except Exception as e:
            log.exception("on-demand refresh failed")
            error = f"Refresh failed: {e}"
    finally:
        conn.close()
    rows = _filter_and_sort(_load_notifications(), _filters_from_request())
    return render_template("_table.html", notifications=rows, error=error)


@app.post("/backfetch")
def backfetch():
    """Temporary: pull last 20 notifications and force re-enrichment."""
    token = app.config["GITHUB_TOKEN"]
    error: str | None = None
    conn = db.connect()
    try:
        try:
            github.backfetch(conn, token, n=20)
        except Exception as e:
            log.exception("backfetch failed")
            error = f"Backfetch failed: {e}"
    finally:
        conn.close()
    rows = _filter_and_sort(_load_notifications(), _filters_from_request())
    return render_template("_table.html", notifications=rows, error=error)


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


@app.post("/toggle/<thread_id>/read")
def toggle_read(thread_id: str):
    """Toggle local read state. Marking-read pushes to GitHub; un-marking is local-only."""
    n = _load_one(thread_id)
    if not n:
        return ("", 404)
    if n["unread"]:
        github.mark_read(app.config["GITHUB_TOKEN"], thread_id)
        _apply_action(thread_id, "read", unread=0)
    else:
        # GitHub's REST/GraphQL API has no "mark unread" — only the web UI can,
        # via /notifications/beta/unmark (session-cookie + CSRF auth, NT_* node
        # IDs, undocumented). Not worth implementing. Mark locally and signal
        # reconciliation to skip via action='kept_unread'.
        _apply_action(thread_id, "kept_unread", unread=1)
    return render_template("_row.html", n=_load_one(thread_id))


@app.post("/toggle/<thread_id>/unsub")
def toggle_unsub(thread_id: str):
    """Toggle ignored state on the thread subscription. Ignoring also marks read."""
    token = app.config["GITHUB_TOKEN"]
    n = _load_one(thread_id)
    if not n:
        return ("", 404)
    if n["ignored"]:
        github.set_subscribed(token, thread_id)
        _apply_action(thread_id, "subscribed", ignored=0)
    else:
        github.set_ignored(token, thread_id)
        github.mark_read(token, thread_id)  # unsub implies read (not done)
        _apply_action(thread_id, "unsub", ignored=1, unread=0)
    return render_template("_row.html", n=_load_one(thread_id))


@app.post("/action/<thread_id>/done")
def action_done(thread_id: str):
    github.mark_done(app.config["GITHUB_TOKEN"], thread_id)
    _apply_action(thread_id, "done", unread=0)
    return ("", 200)
