"""AI v0: manually-triggered, approve-based per-thread judgment.

User clicks "Ask AI" on a row → judge() generates a structured verdict and
caches it on the row. User clicks Approve → apply_verdict() executes the
proposed mutations using the same GitHub + DB calls the manual buttons use.
User clicks Dismiss → dismiss_verdict() clears the cache without mutating.

No autonomous re-judgment in v0; the per-thread `policy` field from the
original design is intentionally absent. Add it back when auto re-judging
ships.

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
from datetime import date, datetime
from pathlib import Path

import anthropic

from . import db, github

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


# Vocabulary of "relevant signals" the AI can flag for surface in the
# Relevance column. Listed here as the source of truth — the schema enum
# below is generated from this list, and the rendering map in
# app/web.py:_SIGNAL_LABELS is keyed by these strings. Adding a new key
# requires touching both files.
SIGNAL_VOCAB = (
    # Action-required signals (typically high priority)
    "review_you", "review_team", "assigned", "mentioned",
    # PR review state
    "approved", "changes_requested",
    # Merge state warnings
    "merge_dirty", "merge_unstable", "merge_behind",
    # Activity / freshness
    "new_comments",
    # Reception flavors (mutually exclusive — pick one)
    "popular", "controversial", "engaged",
    # Lifecycle signals
    "merged", "closed", "answered", "draft",
    # Identity / tracking
    "tracked_author", "tracked_repo", "tracked_org",
    "bot_author", "first_timer",
    # Diff size hints
    "large_diff", "small_diff",
)


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
                "enum": ["none", "mark_read", "mute", "archive"],
                "description": (
                    "What state change to propose (executed only on user approval). "
                    "Prefer 'none' over 'mark_read' over 'archive' when uncertain."
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
                    "How important is this thread to the user, on a 0.0-1.0 scale. "
                    "See system prompt §Priority for anchored value examples. "
                    "Distribute meaningfully — don't cluster around 0.5. "
                    "Independent of action_now: 0.9 + 'none' means 'leave it but flag it as urgent'."
                ),
            },
            "relevant_signals": {
                "type": "array",
                "items": {"type": "string", "enum": list(SIGNAL_VOCAB)},
                "maxItems": 3,
                "description": (
                    "Up to 3 signal keys, in descending order of relevance, that explain why this thread matters. "
                    "The app renders these in the Relevance column. Pick only signals the user should actually weigh — "
                    "not every applicable signal. Empty list is valid (and correct for routine noise)."
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
        "required": ["action_now", "set_tracked", "priority_score", "relevant_signals", "description"],
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
            "Call judge_thread exactly once with action_now, set_tracked, priority_score, relevant_signals, description."
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
        out["requested_reviewers"] = [
            (r or {}).get("login") for r in (d.get("requested_reviewers") or []) if (r or {}).get("login")
        ]
        out["requested_teams"] = [
            (t or {}).get("slug") for t in (d.get("requested_teams") or []) if (t or {}).get("slug")
        ]
    if subject_type == "Discussion":
        out["category"] = d.get("category")
    body = d.get("body") or ""
    if body:
        out["body"] = body[:_BODY_TRUNC] + ("…[truncated]" if len(body) > _BODY_TRUNC else "")
    # Drop None values to keep the request lean and the cache prefix tighter.
    return {k: v for k, v in out.items() if v not in (None, [], "")}


def _load_thread_context(conn: sqlite3.Connection, thread_id: str) -> dict | None:
    """Build the full context dict for one thread. Returns None if not found.
    Mirrors the data web._row_to_dict surfaces in the UI, so the AI sees the
    same picture the user does — just structured."""
    row = conn.execute(
        """
        SELECT id, repo, type, title, reason, html_url, link_url,
               updated_at, last_read_at, unread, ignored, action,
               is_tracked, note_user,
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
        "note_user":     row["note_user"] or None,
    }
    notification = {k: v for k, v in notification.items() if v not in (None, "", False)}

    return {
        "notification": notification,
        "item": item,
        "activity": activity,
        "entities": entities,
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
) -> None:
    conn.execute(
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


def _save_verdict(
    conn: sqlite3.Connection, thread_id: str, verdict: dict, model: str
) -> None:
    conn.execute(
        """
        UPDATE notifications
           SET ai_verdict_json = ?, ai_verdict_at = ?, ai_verdict_model = ?
         WHERE id = ?
        """,
        (json.dumps(verdict, ensure_ascii=False), int(time.time()), model, thread_id),
    )


def _clear_verdict(conn: sqlite3.Connection, thread_id: str) -> None:
    conn.execute(
        """
        UPDATE notifications
           SET ai_verdict_json = NULL, ai_verdict_at = NULL, ai_verdict_model = NULL
         WHERE id = ?
        """,
        (thread_id,),
    )


# ---- Public API ---------------------------------------------------------

class AIError(RuntimeError):
    """Raised when judge() or apply_verdict() can't proceed.
    Routes catch this and surface via the existing showError HX-Trigger."""


def judge(
    thread_id: str,
    conn: sqlite3.Connection,
    *,
    user_login: str | None = None,
    user_teams=None,
) -> dict:
    """Generate a verdict for one thread, persist it on the row, log the
    call, and return the verdict dict. Raises AIError on failure.

    user_login / user_teams come from auth.fetch_identity at startup
    (stored in app.config); when present, they're prepended to the system
    prompt so the model can recognize the user's own login in author /
    assignee / reviewer / commenter fields. Both default to None so this
    module remains callable without web's app context."""
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
    required = {"action_now", "set_tracked", "priority_score", "relevant_signals", "description"}
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
    _log_call(
        conn, thread_id=thread_id, model=model,
        request=request_log, response=response_dict, usage=usage,
        cost_usd=cost, error=None, status="ok",
    )
    return verdict


def _set_state(
    conn: sqlite3.Connection,
    thread_id: str,
    *,
    action: str,
    source: str = "ai",
    **state,
) -> None:
    """Mirrors web._apply_action without importing web (which would import
    Flask). Records action + actioned_at + action_source plus any state cols."""
    cols = {
        "action": action,
        "actioned_at": int(time.time()),
        "action_source": source,
        **state,
    }
    setters = ", ".join(f"{k} = ?" for k in cols)
    values = (*cols.values(), thread_id)
    conn.execute(f"UPDATE notifications SET {setters} WHERE id = ?", values)


def apply_verdict(thread_id: str, conn: sqlite3.Connection, token: str) -> None:
    """Execute the cached verdict's proposed mutations, then clear the
    verdict cache. Idempotent: a no-op if there's no pending verdict.
    Raises AIError if the verdict is malformed."""
    row = conn.execute(
        """
        SELECT ai_verdict_json, unread, ignored, is_tracked
          FROM notifications WHERE id = ?
        """,
        (thread_id,),
    ).fetchone()
    if row is None or not row["ai_verdict_json"]:
        return  # nothing to apply

    try:
        verdict = json.loads(row["ai_verdict_json"])
    except (ValueError, TypeError) as e:
        raise AIError(f"Stored verdict isn't valid JSON: {e}") from e

    action_now = verdict.get("action_now")
    set_tracked = verdict.get("set_tracked")
    # description is the new single-field replacement for summary+rationale.
    # Legacy verdicts (cached before the schema change) still have the old
    # split — fall back to joining them so a pending verdict from an older
    # judgment still applies cleanly.
    description = (verdict.get("description") or "").strip()
    if not description and (verdict.get("summary") or verdict.get("rationale")):
        s = (verdict.get("summary") or "").strip()
        r = (verdict.get("rationale") or "").strip()
        description = " — ".join(p for p in (s, r) if p)
    description = description or None

    state_updates: dict = {}

    if action_now == "mark_read":
        if row["unread"]:
            github.mark_read(token, thread_id)
        if row["ignored"]:
            # Reading a muted thread also unmutes — matches the manual flow.
            github.set_subscribed(token, thread_id)
        state_updates.update(unread=0, ignored=0)
        action_label = "read"
    elif action_now == "mute":
        if row["unread"]:
            github.mark_read(token, thread_id)
        github.set_ignored(token, thread_id)
        state_updates.update(unread=0, ignored=1)
        action_label = "muted"
    elif action_now == "archive":
        github.mark_done(token, thread_id)
        state_updates.update(unread=0)
        action_label = "done"
    elif action_now == "none":
        action_label = None
    else:
        raise AIError(f"Unknown action_now: {action_now!r}")

    # Item-level tracked toggle. People/repo/org tracking is out of scope
    # for v0 (per the "changes to the notification and/or item at hand"
    # constraint).
    if set_tracked == "track":
        state_updates["is_tracked"] = 1
    elif set_tracked == "untrack":
        state_updates["is_tracked"] = 0
    elif set_tracked not in (None, "leave"):
        raise AIError(f"Unknown set_tracked: {set_tracked!r}")

    # The AI description lands in note_ai (separate from the user's note_user).
    if description:
        state_updates["note_ai"] = description

    if action_label is not None:
        _set_state(conn, thread_id, action=action_label, source="ai", **state_updates)
    elif state_updates:
        # No action_now change but tracked / note_ai may still need updating.
        # Don't touch action / actioned_at / action_source in this branch —
        # the user's prior action remains the most recent action of record.
        setters = ", ".join(f"{k} = ?" for k in state_updates)
        values = (*state_updates.values(), thread_id)
        conn.execute(f"UPDATE notifications SET {setters} WHERE id = ?", values)

    _clear_verdict(conn, thread_id)


def dismiss_verdict(thread_id: str, conn: sqlite3.Connection) -> None:
    """Clear the cached verdict without applying any mutations."""
    _clear_verdict(conn, thread_id)
