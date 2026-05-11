"""AI v1: manually-triggered, advisory per-thread judgment.

User clicks "Ask AI" on a row → judge() generates a structured verdict and
caches it on the row + appends it to the per-thread timeline. The verdict
is advisory only: the pill and priority color shape display, but
no row state changes automatically. The user takes their own row actions
(visit, mark read, mute, archive, track); those land as `user_action`
events in the timeline, so the next judgment sees what the user actually
did after the last verdict and can recalibrate.

Re-ask re-runs judge() with the prior verdict still visible in the
timeline (mode=`re_evaluate`); chat re-runs it with the user's latest
`user_chat` event appended (mode=`chat`). The cache is never auto-cleared.

Caching note: the system block is split into [system_prompt, prefs] with
ephemeral cache_control on both. Haiku 4.5's minimum cacheable prefix is
4096 tokens, so caching only fires once the user's preferences grow large
enough to push the combined prefix past that bar. The markers are
forward-compatible — the plumbing is there, the savings show up
automatically once the prefix qualifies. See ai_calls.cache_read_tokens.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from datetime import date, datetime, timezone
from pathlib import Path

import anthropic

from . import db

log = logging.getLogger(__name__)


# Haiku 4.5 doesn't support `effort` and isn't documented for adaptive
# thinking, so we use the older explicit-budget form. budget_tokens must be
# strictly < max_tokens. We give the model a meaningful budget for this
# task — the verdict requires reasoning across multiple signals (tracked
# flags, recent activity, user preferences) and cheaping out on thinking
# tokens shows up immediately as worse rationales.
DEFAULT_MODEL = "claude-haiku-4-5"
DEFAULT_MAX_TOKENS = 8192
DEFAULT_THINKING_BUDGET = 4000
DEFAULT_DAILY_CAP_USD = 2.0

# Per-1M-token prices in USD. Cache writes are ~1.25× input on a 5-minute
# TTL; cache reads are ~0.1× input. These are estimates for cost logging,
# not authoritative billing — Anthropic's invoice is the source of truth.
_PRICES = {
    "claude-haiku-4-5":  {"input": 1.0,  "cache_write": 1.25, "cache_read": 0.1,  "output": 5.0},
    "claude-sonnet-4-6": {"input": 3.0,  "cache_write": 3.75, "cache_read": 0.3,  "output": 15.0},
    "claude-opus-4-7":   {"input": 5.0,  "cache_write": 6.25, "cache_read": 0.5,  "output": 25.0},
    "claude-opus-4-6":   {"input": 5.0,  "cache_write": 6.25, "cache_read": 0.5,  "output": 25.0},
}


# The judge_thread tool. Forced via the system prompt + only-tool-defined
# pattern; its arguments are the verdict.
TOOL_DEF: dict = {
    "name": "judge_thread",
    "description": (
        "Record your triage verdict for the thread shown in the user message. "
        "Call this tool exactly once. Do not produce any other output."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action_now": {
                "type": "string",
                "enum": ["look", "ignore", "mute", "archive", "snooze"],
                "description": (
                    "What you suggest the user do with this thread. "
                    "'look' (open the link), 'ignore' (mark read without engaging), "
                    "'mute' (silence further updates), 'archive' (nothing left to do), "
                    "'snooze' (nothing to do *now* but it won't stay quiet — hide it until ~snooze_days from now; "
                    "blocked-on-someone, scheduled-for-later). "
                    "Advisory only — the user takes their own row actions; nothing auto-applies. "
                    "Prefer 'look' over 'ignore' over 'archive' when uncertain; reach for 'snooze' only with a concrete reason it'll be quiet until then."
                ),
            },
            "snooze_days": {
                "type": "integer",
                "minimum": 1,
                "maximum": 90,
                "description": (
                    "Only when action_now is 'snooze': roughly how many days until the thread is worth another look "
                    "(your best estimate — e.g. a review the user is waiting on a teammate for, a release dated ~3 weeks out). "
                    "Ignored for any other action_now."
                ),
            },
            "set_tracked": {
                "type": "string",
                "enum": ["track", "untrack", "leave"],
                "description": (
                    "Whether to change the item-level tracked flag. "
                    "Default to 'leave' unless preferences clearly say to track / untrack this kind of thread."
                ),
            },
            "priority_score": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": (
                    "How urgently the user should deal with this thread, on a 0.0-1.0 scale. "
                    "See system prompt §Priority for the named bands each range maps to — "
                    "pick a value inside the band that fits, or between bands when it's on the edge. "
                    "Independent of action_now: 0.9 + 'look' means 'leave it visible and flag it as urgent'."
                ),
            },
            "description": {
                "type": "string",
                "description": (
                    "Interpretation of the thread (not restatement of row-visible facts). "
                    "See system prompt §Brevity for length and content rules."
                ),
            },
        },
        "required": ["action_now", "set_tracked", "priority_score", "description"],
    },
}


# ---- Prompt assembly ----------------------------------------------------

_SYSTEM_PROMPT_PATH = Path(__file__).parent / "ai_system_prompt.md"
_PREFERENCES_PATH = Path("config") / "preferences.md"


def _read_system_prompt() -> str:
    try:
        return _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        log.warning("ai_system_prompt.md not found; using minimal fallback")
        return (
            "You are a GitHub notification triage assistant. "
            "Call judge_thread exactly once with action_now, set_tracked, priority_score, description."
        )


def _read_preferences() -> str:
    """User-edited preferences. Empty when the user hasn't created the file."""
    try:
        return _PREFERENCES_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return "(No preferences file found at config/preferences.md. Judge using the heuristics in the system prompt only.)"


def _identity_block(user_login: str | None, user_teams) -> str | None:
    """Short prefix telling the model whose inbox it's triaging. Without it,
    the AI sees logins like 'lukastenbrink' as anonymous strings and can't
    tell author/assignee/reviewer fields apart from any other commenter.
    Returns None when the login isn't known (auth.fetch_identity failed)."""
    if not user_login:
        return None
    parts = [
        f"You are judging on behalf of GitHub user @{user_login}. "
        f'Treat the login "{user_login}" appearing in any field (author, assignee, '
        f"requested_reviewer, commenter, etc.) as the user themselves."
    ]
    if user_teams:
        team_strs = sorted(f"{org}/{slug}" for org, slug in user_teams)
        parts.append(
            "The user belongs to these GitHub teams: "
            + ", ".join(team_strs)
            + ". A requested_teams entry matching one of these is a team-level review request to the user."
        )
    return " ".join(parts)


# ---- Context assembly ---------------------------------------------------

# Body truncation limit when building the per-thread context. Sized to fit
# virtually all human-authored PR / issue / discussion bodies (typical max
# is ~10KB even for RFC-style threads); the cap is a guardrail against
# pathological auto-generated walls (release-please changelogs, dependabot
# bundle reports) that would otherwise dominate the prompt.
_BODY_TRUNC = 32000

# Fields we extract from details_json. Listed explicitly rather than
# passing the whole blob so the model isn't trained on incidental schema
# drift, and so the request is small enough to actually fit in the cache.
def _summarize_details(d: dict, subject_type: str) -> dict:
    if not d:
        return {}
    out: dict = {
        "state":               d.get("state"),
        "draft":               d.get("draft"),
        "merged":              d.get("merged"),
        "merged_at":           d.get("merged_at"),
        "closed_at":           d.get("closed_at"),
        "created_at":          d.get("created_at"),
        "author_login":        (d.get("user") or {}).get("login"),
        "author_association":  d.get("author_association"),
        "comments":            d.get("comments"),
        "labels":              [l.get("name") for l in (d.get("labels") or []) if l.get("name")],
        "assignees":           [(a or {}).get("login") for a in (d.get("assignees") or []) if (a or {}).get("login")],
    }
    if subject_type == "PullRequest":
        out["additions"] = d.get("additions")
        out["deletions"] = d.get("deletions")
        out["changed_files"] = d.get("changed_files")
        out["mergeable_state"] = d.get("mergeable_state")
        # Latest head commit — "when did the code last change", distinct from
        # updatedAt (which also moves on comments / labels). {abbrev_oid,
        # message, committed_at, author, total}. None until first enrichment.
        out["last_commit"] = d.get("last_commit")
        out["requested_reviewers"] = [
            (r or {}).get("login") for r in (d.get("requested_reviewers") or []) if (r or {}).get("login")
        ]
        out["requested_teams"] = [
            (t or {}).get("slug") for t in (d.get("requested_teams") or []) if (t or {}).get("slug")
        ]
        # Per-file diff stats as a compact string list — "path +N/-M". String
        # form is ~2.5x cheaper in tokens than the equivalent JSON objects,
        # which matters because file lists can run to 100 entries on big PRs.
        # Truncation: GraphQL caps at 100 files; the AI compares list length
        # against `changed_files` (total) to know when it's seeing a subset.
        files = d.get("files") or []
        if files:
            out["files"] = [
                f"{f.get('filename')} +{f.get('additions') or 0}/-{f.get('deletions') or 0}"
                for f in files if f.get("filename")
            ]
    if subject_type == "Discussion":
        out["category"] = d.get("category")
    # Comment + review bodies are no longer summarized here — they're
    # first-class events in `timeline` (see _load_timeline / thread_events).
    body = d.get("body") or ""
    if body:
        out["body"] = body[:_BODY_TRUNC] + ("…[truncated]" if len(body) > _BODY_TRUNC else "")
    # Drop None values to keep the request lean and the cache prefix tighter.
    return {k: v for k, v in out.items() if v not in (None, [], "")}


def _load_timeline(conn: sqlite3.Connection, thread_id: str) -> list[dict]:
    """Return the chronological per-thread event timeline, oldest first.
    `at` is rendered as ISO 8601 UTC so the model can compute deltas
    against the `now` field added at the top of the user message.

    No truncation in v1 — token budget is comfortable on Haiku without
    compression. When threads grow long enough to warrant it, the hook
    is to write `kind=ai_recap` events that summarize older history."""
    rows = conn.execute(
        """
        SELECT ts, kind, source, external_id, payload_json
          FROM thread_events
         WHERE thread_id = ?
         ORDER BY ts ASC, id ASC
        """,
        (thread_id,),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        try:
            payload = json.loads(r["payload_json"])
        except (ValueError, TypeError):
            payload = {}
        out.append({
            "at": datetime.fromtimestamp(r["ts"], tz=timezone.utc).isoformat().replace("+00:00", "Z"),
            "kind": r["kind"],
            "source": r["source"],
            "payload": payload,
        })
    return out


def _load_thread_context(conn: sqlite3.Connection, thread_id: str) -> dict | None:
    """Build the full context dict for one thread. Returns None if not found.
    Mirrors the data web._row_to_dict surfaces in the UI, so the AI sees the
    same picture the user does — just structured."""
    row = conn.execute(
        """
        SELECT id, repo, type, title, reason, html_url, link_url,
               updated_at, last_read_at, unread, ignored, action,
               is_tracked,
               details_json, seen_reasons,
               baseline_comments, baseline_review_state, pr_review_state,
               unique_commenters, unique_reviewers, pr_reactions_json
          FROM notifications WHERE id = ?
        """,
        (thread_id,),
    ).fetchone()
    if row is None:
        return None

    repo = row["repo"] or ""
    repo_owner, _, repo_name = repo.partition("/")

    details: dict = {}
    if row["details_json"]:
        try:
            details = json.loads(row["details_json"])
        except (ValueError, TypeError):
            pass
    seen: list[str] = []
    if row["seen_reasons"]:
        try:
            seen = list(json.loads(row["seen_reasons"]))
        except (ValueError, TypeError):
            pass
    pr_reactions: dict | None = None
    if row["pr_reactions_json"]:
        try:
            pr_reactions = json.loads(row["pr_reactions_json"])
        except (ValueError, TypeError):
            pass

    item = _summarize_details(details, row["type"])
    if pr_reactions:
        item["reactions"] = pr_reactions
    elif details.get("reactions"):
        item["reactions"] = details["reactions"]

    # New comments since baseline. baseline_comments is captured lazily on
    # first enrichment (see app/github.py); a delta here is "what's new
    # since the user last looked".
    baseline = row["baseline_comments"]
    comments = item.get("comments")
    new_comments = None
    if isinstance(comments, int) and isinstance(baseline, int) and comments > baseline:
        new_comments = comments - baseline

    activity = {
        "baseline_comments": baseline,
        "new_comments_since_baseline": new_comments,
        "seen_reasons": seen,
        "mentioned_since": ("mention" in seen) or ("team_mention" in seen),
        "pr_review_state": row["pr_review_state"],
        "baseline_review_state": row["baseline_review_state"],
        "review_state_changed":
            bool(row["pr_review_state"]) and row["pr_review_state"] != row["baseline_review_state"],
        "unique_commenters": row["unique_commenters"],
        "unique_reviewers": row["unique_reviewers"],
    }
    activity = {k: v for k, v in activity.items() if v not in (None, [], False)}

    # Entity notes: author, repo, org. Only the three relevant to this
    # thread, not the whole table.
    author_login = item.get("author_login")
    entities: dict = {}
    if author_login:
        person = conn.execute(
            "SELECT login, is_tracked, note_user FROM people WHERE login = ?",
            (author_login,),
        ).fetchone()
        entities["author"] = {
            "login": author_login,
            "is_tracked": bool(person and person["is_tracked"]),
            "note_user": (person["note_user"] if person else None) or None,
        }
    if repo:
        rrow = conn.execute(
            "SELECT name, is_tracked, note_user FROM repos WHERE name = ?",
            (repo,),
        ).fetchone()
        entities["repo"] = {
            "name": repo,
            "is_tracked": bool(rrow and rrow["is_tracked"]),
            "note_user": (rrow["note_user"] if rrow else None) or None,
        }
    if repo_owner:
        orow = conn.execute(
            "SELECT name, is_tracked, note_user FROM orgs WHERE name = ?",
            (repo_owner,),
        ).fetchone()
        entities["org"] = {
            "name": repo_owner,
            "is_tracked": bool(orow and orow["is_tracked"]),
            "note_user": (orow["note_user"] if orow else None) or None,
        }
    # Drop entities with no signal (no track flag, no note) so the prompt
    # is shorter and the AI doesn't read into a row of empty defaults.
    entities = {
        k: v for k, v in entities.items()
        if v.get("is_tracked") or v.get("note_user")
    }

    notification = {
        "id":            row["id"],
        "repo":          repo,
        "type":          row["type"],
        "title":         row["title"],
        "reason":        row["reason"],
        "html_url":      row["html_url"],
        "link_url":      row["link_url"],
        "updated_at":    row["updated_at"],
        "unread":        bool(row["unread"]),
        "ignored":       bool(row["ignored"]),
        "action":        row["action"],
        "is_tracked":    bool(row["is_tracked"]),
    }
    notification = {k: v for k, v in notification.items() if v not in (None, "", False)}

    timeline = _load_timeline(conn, thread_id)
    now_iso = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")

    return {
        "now": now_iso,
        "notification": notification,
        "item": item,
        "activity": activity,
        "entities": entities,
        "timeline": timeline,
    }


def _build_user_message(ctx: dict) -> str:
    """Render the per-thread context as a compact JSON payload.
    Sorted keys so the same row produces the same bytes — important for
    later cache hits on re-judgment."""
    return json.dumps(ctx, sort_keys=True, indent=2, ensure_ascii=False)


# ---- Cost + cap ---------------------------------------------------------

def _estimate_cost(model: str, usage: dict) -> float:
    p = _PRICES.get(model)
    if not p:
        return 0.0
    inp   = (usage.get("input_tokens") or 0)
    cw    = (usage.get("cache_creation_input_tokens") or 0)
    cr    = (usage.get("cache_read_input_tokens") or 0)
    out   = (usage.get("output_tokens") or 0)
    return (
        inp * p["input"]
        + cw * p["cache_write"]
        + cr * p["cache_read"]
        + out * p["output"]
    ) / 1_000_000


def _spent_today(conn: sqlite3.Connection) -> float:
    """Sum of cost_usd across today's ai_calls (local timezone). Includes
    failed calls too — a 200 that we then errored on locally still cost
    money. cap_exceeded rows have cost_usd=0 by construction."""
    start = int(datetime.combine(date.today(), datetime.min.time()).timestamp())
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) AS s FROM ai_calls WHERE created_at >= ?",
        (start,),
    ).fetchone()
    return float(row["s"] or 0.0)


def _daily_cap() -> float:
    raw = os.environ.get("AI_DAILY_CAP_USD")
    if raw is None:
        return DEFAULT_DAILY_CAP_USD
    try:
        return float(raw)
    except ValueError:
        log.warning("AI_DAILY_CAP_USD=%r is not a number; using default", raw)
        return DEFAULT_DAILY_CAP_USD


def _model_id() -> str:
    return os.environ.get("AI_MODEL", DEFAULT_MODEL)


# ---- Persistence helpers ------------------------------------------------

def _log_call(
    conn: sqlite3.Connection,
    *,
    thread_id: str,
    model: str,
    request: dict,
    response: dict | None,
    usage: dict | None,
    cost_usd: float,
    error: str | None,
    status: str,
) -> int:
    """Insert an ai_calls row and return its id. Callers on the success
    path use the id as the external_id of the resulting ai_verdict
    thread_event so the timeline can join back to the full request /
    response pair for audit + prompt tuning."""
    cursor = conn.execute(
        """
        INSERT INTO ai_calls (
            thread_id, created_at, model, request_json, response_json,
            input_tokens, cache_read_tokens, cache_creation_tokens,
            output_tokens, cost_usd, error, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            thread_id,
            int(time.time()),
            model,
            json.dumps(request, ensure_ascii=False),
            json.dumps(response, ensure_ascii=False, default=str) if response is not None else None,
            (usage or {}).get("input_tokens"),
            (usage or {}).get("cache_read_input_tokens"),
            (usage or {}).get("cache_creation_input_tokens"),
            (usage or {}).get("output_tokens"),
            cost_usd,
            error,
            status,
        ),
    )
    return int(cursor.lastrowid)


def _save_verdict(
    conn: sqlite3.Connection, thread_id: str, verdict: dict, model: str
) -> None:
    # A fresh verdict reclaims the displayed priority: clear any hand-set
    # priority_user. The user's choice isn't lost — it survives as a
    # priority_change event in the timeline, which this judgment already
    # saw and folded into priority_score. The user can re-pin afterwards.
    conn.execute(
        """
        UPDATE notifications
           SET ai_verdict_json = ?, ai_verdict_at = ?, ai_verdict_model = ?,
               priority_user = NULL
         WHERE id = ?
        """,
        (json.dumps(verdict, ensure_ascii=False), int(time.time()), model, thread_id),
    )


# ---- Public API ---------------------------------------------------------

class AIError(RuntimeError):
    """Raised when judge() can't proceed.
    Routes catch this and surface via the existing showError HX-Trigger."""


def judge(
    thread_id: str,
    conn: sqlite3.Connection,
    *,
    user_login: str | None = None,
    user_teams=None,
    invocation_mode: str = "summary",
) -> dict:
    """Generate a verdict for one thread, persist it on the row, log the
    call, and return the verdict dict. Raises AIError on failure.

    user_login / user_teams come from auth.fetch_identity at startup
    (stored in app.config); when present, they're prepended to the system
    prompt so the model can recognize the user's own login in author /
    assignee / reviewer / commenter fields. Both default to None so this
    module remains callable without web's app context.

    invocation_mode — one of `summary` / `re_evaluate` / `chat`. Surfaced
    in the user message so the system prompt can branch on what tone
    the description should take (see ai_system_prompt.md §Invocation
    modes). Defaults to `summary` for callers that don't supply it."""
    model = _model_id()
    cap = _daily_cap()
    spent = _spent_today(conn)

    if spent >= cap:
        msg = f"Daily AI cap of ${cap:.2f} reached (${spent:.4f} spent). Set AI_DAILY_CAP_USD higher to continue."
        _log_call(
            conn, thread_id=thread_id, model=model,
            request={}, response=None, usage=None, cost_usd=0.0,
            error=msg, status="cap_exceeded",
        )
        raise AIError(msg)

    ctx = _load_thread_context(conn, thread_id)
    if ctx is None:
        raise AIError(f"Thread {thread_id} not found")
    ctx["invocation_mode"] = invocation_mode

    system_prompt = _read_system_prompt()
    identity = _identity_block(user_login, user_teams)
    if identity:
        system_prompt = f"{identity}\n\n{system_prompt}"
    prefs = _read_preferences()
    user_msg = _build_user_message(ctx)

    request_log = {
        "model": model,
        "max_tokens": DEFAULT_MAX_TOKENS,
        "thinking": {"type": "enabled", "budget_tokens": DEFAULT_THINKING_BUDGET},
        # tool_choice is intentionally NOT forced. Anthropic rejects the
        # combination of extended thinking + forced tool_choice with
        # "Thinking may not be enabled when tool_choice forces tool use."
        # We keep thinking (deliberation matters more than the marginal
        # safety net of forcing) and rely on the system prompt + the fact
        # that there's exactly one tool defined to make Claude call it.
        # If it doesn't, judge() raises AIError below; the route surfaces
        # it as a red toast and no verdict is cached.
        # System and user content are logged in full so we can replay the
        # exact prompt later when tuning. Static content (system prompt,
        # prefs) is repeated across rows; that's the price of being able
        # to audit individual calls.
        "system": [system_prompt, prefs],
        "user_message": user_msg,
        "tools": [TOOL_DEF],
    }

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        msg = "ANTHROPIC_API_KEY not set"
        _log_call(
            conn, thread_id=thread_id, model=model,
            request=request_log, response=None, usage=None, cost_usd=0.0,
            error=msg, status="error",
        )
        raise AIError(msg)

    client = anthropic.Anthropic(api_key=api_key)

    try:
        response = client.messages.create(
            model=model,
            max_tokens=DEFAULT_MAX_TOKENS,
            thinking={"type": "enabled", "budget_tokens": DEFAULT_THINKING_BUDGET},
            system=[
                {"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": prefs, "cache_control": {"type": "ephemeral"}},
            ],
            tools=[TOOL_DEF],
            messages=[{"role": "user", "content": user_msg}],
        )
    except anthropic.APIError as e:
        msg = f"{type(e).__name__}: {e}"
        _log_call(
            conn, thread_id=thread_id, model=model,
            request=request_log, response=None, usage=None, cost_usd=0.0,
            error=msg, status="error",
        )
        raise AIError(msg) from e

    # Find the judge_thread tool_use block. With tool_choice forcing it,
    # there's exactly one — but we still defend against drift.
    tool_use = next(
        (b for b in response.content if getattr(b, "type", None) == "tool_use"
         and getattr(b, "name", None) == "judge_thread"),
        None,
    )
    response_dict = response.model_dump() if hasattr(response, "model_dump") else None
    usage = (response_dict or {}).get("usage") or {}
    cost = _estimate_cost(model, usage)

    if tool_use is None:
        msg = "Model did not call judge_thread"
        _log_call(
            conn, thread_id=thread_id, model=model,
            request=request_log, response=response_dict, usage=usage,
            cost_usd=cost, error=msg, status="error",
        )
        raise AIError(msg)

    verdict = dict(tool_use.input or {})
    required = {"action_now", "set_tracked", "priority_score", "description"}
    missing = required - verdict.keys()
    if missing:
        msg = f"Verdict missing fields: {sorted(missing)}"
        _log_call(
            conn, thread_id=thread_id, model=model,
            request=request_log, response=response_dict, usage=usage,
            cost_usd=cost, error=msg, status="error",
        )
        raise AIError(msg)

    _save_verdict(conn, thread_id, verdict, model)
    ai_call_id = _log_call(
        conn, thread_id=thread_id, model=model,
        request=request_log, response=response_dict, usage=usage,
        cost_usd=cost, error=None, status="ok",
    )
    # Append the verdict to the per-thread timeline. external_id joins
    # back to ai_calls so the full request / response is one query away.
    # `model` is folded into the payload so timeline-render of past
    # verdicts can show which model produced each one.
    db.write_thread_event(
        conn,
        thread_id=thread_id,
        ts=int(time.time()),
        kind="ai_verdict",
        source="ai",
        external_id=str(ai_call_id),
        payload={**verdict, "model": model},
    )
    return verdict
