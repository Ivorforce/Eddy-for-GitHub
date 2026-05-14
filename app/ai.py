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
On a re-judgment the model can be economical: `judge_thread` with the
standing fields (`disposition` / `priority_score` / `description`) omitted
to keep the last verdict's values, or — on a plain Re-ask — a single
`skip` call that re-affirms the prior verdict wholesale. Either way
`_save_verdict` stores the fully-merged result, so the cached verdict is
always complete (and the next re-judgment inherits from a complete base).

Caching note: the system block is split into [system_prompt, prefs] with
ephemeral cache_control on both. The system prompt alone sits comfortably
past Haiku 4.5's 4096-token cache minimum, so caching fires routinely —
the cached prefix is ~10-12K tokens, cache hits read at 10× cheaper than
fresh input. Cache misses are the 5-min ephemeral TTL expiring between
bursts of re-asks. See ai_calls.cache_read_tokens / cache_creation_tokens.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path

import anthropic

from . import db, github, settings

log = logging.getLogger(__name__)


# Haiku 4.5 doesn't support `effort` and isn't documented for adaptive
# thinking, so we use the older explicit-budget form. budget_tokens must be
# strictly < max_tokens. We give the model a meaningful budget for this
# task — the verdict requires reasoning across multiple signals (tracked
# flags, recent activity, user preferences) and cheaping out on thinking
# tokens shows up immediately as worse rationales. Thinking is the single
# biggest controllable slice of a call's cost (~40% of a cache-warm call),
# so it's skipped on the one boring re-judgment — a Re-ask folding in a fresh
# code push / metadata churn and nothing else — see _should_think.
DEFAULT_MODEL = "claude-haiku-4-5"
DEFAULT_MAX_TOKENS = 8192
DEFAULT_THINKING_BUDGET = 4000
DEFAULT_DAILY_CAP_USD = 2.0


def has_api_key() -> bool:
    """Cheap gate for the AI triage UI / launch check. Presence only —
    a wrong/expired key still passes here and surfaces on first judge as
    the usual red-toast AIError. Anthropic has no third-party OAuth, so
    the key has to be supplied as `ANTHROPIC_API_KEY` (typically in .env)."""
    return bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip())

# Event kinds that, arriving since the last verdict, mark a re-judgment as
# substantive — there's new discussion / state / a reframed body to weigh, so
# it gets a thinking pass. The *only* no-thinking re-judgment is one with none
# of these since the verdict but a `code` push — the user re-asked to fold in
# a fresh diff, and the current PR file list + diff stats + last_commit are
# right there in the prompt, no scratchpad needed (see _should_think).
# `body_edit` is in the set: the model only ever sees the *current* body,
# never the diff, so an edit it hasn't seen could be a substantial reframe.
# Subset of web._VERDICT_INVALIDATING_KINDS (which adds `code`) — a code-only
# Re-ask still invalidates the verdict, just doesn't earn thinking. Kept local
# to dodge the ai↔web import cycle.
_THINKING_REQUIRED_KINDS = ("comment", "review", "lifecycle", "user_chat", "body_edit")

# Per-1M-token prices in USD. Cache writes are ~1.25× input on a 5-minute
# TTL; cache reads are ~0.1× input. These are estimates for cost logging,
# not authoritative billing — Anthropic's invoice is the source of truth.
_PRICES = {
    "claude-haiku-4-5":  {"input": 1.0,  "cache_write": 1.25, "cache_read": 0.1,  "output": 5.0},
    "claude-sonnet-4-6": {"input": 3.0,  "cache_write": 3.75, "cache_read": 0.3,  "output": 15.0},
    "claude-opus-4-7":   {"input": 5.0,  "cache_write": 6.25, "cache_read": 0.5,  "output": 25.0},
    "claude-opus-4-6":   {"input": 5.0,  "cache_write": 6.25, "cache_read": 0.5,  "output": 25.0},
}


# The judge_thread tool. The model is steered to it by the system prompt
# (and, when extended thinking is off, tool_choice forces *a* tool — judge
# or skip); its arguments are the verdict.
TOOL_DEF: dict = {
    "name": "judge_thread",
    "description": (
        "Record your triage verdict for the thread shown in the user message. "
        "Use exactly one tool call (this or `skip`, when offered); produce no other output."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "disposition": {
                "type": "string",
                "enum": ["look", "queue", "mute", "done", "snooze", "snooze_quiet"],
                "description": (
                    "How the row should be dealt with on the inbox-state axis — see system prompt §Output fields / §Cost asymmetry. "
                    "Row stays visible for 'look' and 'queue'; 'done' / 'snooze' / 'snooze_quiet' / 'mute' hide it (back on activity / on the timer or activity / on the timer only / never). "
                    "'look' (personal engagement required — the user must open the link before this row is handled, because the summary can't do the thread justice or you can't make a confident triage call without their eyes on it); "
                    "'queue' (triaged, no personal engagement required — drops the row into the act-on-it queue marked read, at this priority); "
                    "'done' (the user's part is done and the next move, if any, is someone else's — and they're fine if nothing ever happens again; Done auto-resurfaces on new activity, so the ball coming back brings the row back. NOT 'park it for now'); "
                    "'snooze' (the user's waiting on something and wants a nudge if it hasn't happened by ~snooze_days — a teammate's reply, a dated release, a review due before a meeting; new activity resurfaces it sooner); "
                    "'snooze_quiet' (the thread's a firehose — hide it AND unsubscribe for ~snooze_days, then it comes back re-subscribed for a fresh look; re-doing it each time is a periodic digest. New activity does NOT bring it back early — that's the point. For a flood where even subscription_changes wouldn't quiet it, or when there's just nothing to wait for); "
                    "'mute' (the full opt-out — hides the row like 'done' AND unsubscribes for good, never resurfaces; the 'I never want to see this again'). "
                    "For 'quiet the churn but stay subscribed and visible' (a partial, ongoing filter) use `subscription_changes`, not this. "
                    "All advisory — the user acts (or not) themselves, so don't agonise: when the call is clear, make it (including 'done' on a finished thread and 'mute' on a plainly-irrelevant one). "
                    "Two tie-break axes for genuine uncertainty: on visibility, lean visible ('look' / 'queue') over hidden when unsure whether the row needs to stay in view; on engagement, lean 'look' over 'queue' when unsure whether the user's personal attention is required. Don't 'done' a thread where prolonged silence would matter — that's 'snooze''s timer. "
                    "On a re-judgment, may be omitted to keep your last verdict's value (see system prompt §Output fields)."
                ),
            },
            "snooze_days": {
                "type": "integer",
                "minimum": 1,
                "maximum": 90,
                "description": (
                    "With disposition 'snooze' or 'snooze_quiet': how many days (1-90) until the row should come back — "
                    "for 'snooze', when 'still waiting' should become 'go chase it' (e.g. a review the user awaits a teammate on, a release ~3 weeks out); "
                    "for 'snooze_quiet', the gap between digests. Ignored for any other disposition."
                ),
            },
            "set_tracked": {
                "type": "string",
                "enum": ["track", "untrack"],
                "description": (
                    "Per-turn change to the item-level tracked flag. *Omit* to leave it as-is "
                    "(the default — nothing to do this turn). Use 'track' only when preferences "
                    "explicitly direct, or when a `user_chat` asks to track it; 'untrack' only "
                    "when the user explicitly asks. The user's own use of the flag ('I want to "
                    "come back to this') isn't something you can infer — don't try."
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
                    "Independent of disposition: 0.9 + 'look' means 'leave it visible and flag it as urgent'. "
                    "On a re-judgment, may be omitted to keep your last verdict's value (see §Output fields)."
                ),
            },
            "description": {
                "type": "string",
                "description": (
                    "Interpretation of the thread (not restatement of row-visible facts). "
                    "The standing take, written self-contained — fresh each judgment, never "
                    "referencing your prior verdicts ('unchanged', 'as before'). Never a reply "
                    "to the user — that's `reply`. See system prompt §Brevity for length and "
                    "content rules. On a re-judgment, may be omitted to keep your last "
                    "verdict's description verbatim — but only when you'd write exactly that "
                    "again (see §Output fields)."
                ),
            },
            "reply": {
                "type": "string",
                "description": (
                    "Optional. A direct reply to the user — include it only when a `user_chat` "
                    "message on the thread asks something or wants a response (most often you've "
                    "just been sent one in 'chat' mode). Answer it or push back, concisely. "
                    "Omit entirely when there's nothing to answer; a bare acknowledgement is "
                    "noise. Not a substitute for `description`. See system prompt §Output fields."
                ),
            },
            "reasoning": {
                "type": "string",
                "description": (
                    "Optional. The load-bearing *why* of this verdict — disposition or "
                    "priority — that `description` doesn't carry. NOT user-visible: only "
                    "future-you reads this (via the timeline). Per-turn, never inherited: "
                    "omit when there's nothing fresh to add — prior `reasoning` notes on "
                    "the timeline stay on record and the silence reads as 'still holds'. "
                    "Same brevity as `description`. See system prompt §Output fields."
                ),
            },
        },
        "required": ["disposition", "priority_score", "description"],
    },
}


# The skip tool — re-affirm the cached verdict unchanged. Only offered on a
# re-judgment (re_evaluate, prior verdict present); see judge() / the system
# prompt §Output fields. No arguments: "skip" means "nothing to change".
SKIP_TOOL_DEF: dict = {
    "name": "skip",
    "description": (
        "Re-affirm your existing verdict, unchanged. "
        "Use this on a Re-ask when, after weighing everything that's happened since your "
        "last verdict, your read is the same in every respect: same action, same priority, "
        "the same standing description, nothing to add for subscription_changes. It re-stamps "
        "the prior verdict as current. If *anything* moves — even a priority nudge or a "
        "one-word sharpening of the description — that's a `judge_thread` call instead "
        "(omitting only the fields that genuinely haven't changed). May optionally carry a "
        "fresh `reasoning` (per-turn, AI-only — see `judge_thread.reasoning`). "
        "See system prompt §Output fields."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "reasoning": {
                "type": "string",
                "description": (
                    "Optional. Per-turn AI-only note — see `judge_thread.reasoning`. "
                    "Use sparingly; the skip itself already signals 'nothing changed'."
                ),
            },
        },
    },
}


def _build_tool_def(notif_type: str, muted_kinds, *, has_prior: bool) -> dict:
    """judge_thread, tailored to this thread. The subscription_changes enum
    offers, for each kind that fires on this notification type, `mute_<kind>`
    when it's currently un-muted on the thread else `unmute_<kind>` (so the
    model can't emit a no-op); it's dropped entirely for types with no
    filterable activity (Release, CheckSuite, …). When `has_prior` — there's
    a verdict in the timeline this re-judgment can fall back on — `disposition`,
    `priority_score` and `description` come out of `required`: omitting any of
    them keeps that field's value from the last verdict (see §Output fields).
    `set_tracked` is always optional — it's a per-turn change to the flag, not
    a standing field, and omitting it means 'no change this turn'."""
    schema = copy.deepcopy(TOOL_DEF)
    applicable = github.MUTE_KINDS_BY_TYPE.get(notif_type, ())
    if applicable:
        muted = set(muted_kinds or [])
        tokens = [
            (f"unmute_{k}" if k in muted else f"mute_{k}")
            for k in github.MUTE_KINDS if k in applicable
        ]
        schema["input_schema"]["properties"]["subscription_changes"] = {
            "type": "array",
            "items": {"type": "string", "enum": tokens},
            "description": (
                "Forward-looking subscription tweaks for this thread — quiet (or resume) "
                "individual activity kinds without unsubscribing. Use when one kind dominates "
                "the churn and another carries the signal; pairs with `disposition: queue` "
                "(passive watcher) or `done` (their part is over). Tokens reflect current "
                "state; `mute_<kind>` stops those notifications going forward, `unmute_<kind>` "
                "resumes them. See system prompt §Subscription tweaks."
            ),
        }
    if has_prior:
        schema["input_schema"]["required"] = []
    return schema


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
            "Call judge_thread exactly once with disposition, priority_score, description."
        )


def _read_preferences() -> str:
    """User-edited preferences. Empty when the user hasn't created the file."""
    try:
        return _PREFERENCES_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return "(No preferences file found at config/preferences.md. Judge using the heuristics in the system prompt only.)"


def _identity_block(user_login: str | None, user_teams) -> str | None:
    """Short prefix telling the model whose inbox it's triaging. Without it,
    the AI sees logins like 'octocat' as anonymous strings and can't
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
        # GitHub Project (v2) boards this thread sits on:
        # [{project, fields?: {field_name: value}}]. Advisory — field names /
        # values are team-specific and the card often lags the GitHub state.
        # See app/github.py:_parse_project_items and the system prompt entry.
        "projects":            d.get("projects"),
    }
    if subject_type == "PullRequest":
        out["additions"] = d.get("additions")
        out["deletions"] = d.get("deletions")
        out["changed_files"] = d.get("changed_files")
        out["mergeable_state"] = d.get("mergeable_state")
        # Why a "blocked" PR is blocked: GitHub's review verdict (approved /
        # changes_requested / review_required) + the CI rollup ({state,
        # failing?, pending?}). Both None-dropped below when absent.
        out["review_decision"] = d.get("review_decision")
        out["checks"] = d.get("checks")
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
        # Issues this PR closes on merge ({number, title, state, reactions?})
        # and the inline-review-thread state ({resolved: int, unresolved:
        # [{path, comments, last_comment_at?, outdated?, comments_sample?}]}) —
        # both None-dropped below when absent.
        out["closes"] = d.get("closes")
        out["review_threads"] = d.get("review_threads")
    if subject_type == "Discussion":
        out["category"] = d.get("category")
    if subject_type == "Release":
        out["name"] = d.get("name")
        out["tag_name"] = d.get("tag_name")
        out["prerelease"] = d.get("prerelease")
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


# `ai_verdict` is the cutoff, not content.
def _changes_since_last_verdict(
    conn: sqlite3.Connection, thread_id: str
) -> dict | None:
    """Mechanical histogram of timeline events after the most recent
    `ai_verdict` event (a `skip` counts — it writes one too), so a re-judgment
    doesn't have to re-derive "what's moved since I last looked" by scanning
    the flat log. Returns None when no prior verdict exists (a first judgment —
    `activity` already covers "new since the *user* last looked", a different
    anchor); returns an empty dict when a verdict exists but nothing's happened
    since (its presence still tells the model "you've judged this before").

    Purely derived from `thread_events`; the `timeline` array stays
    authoritative if they ever disagree. Caveat: event `ts` is the GitHub
    timestamp (`created_at` / `submittedAt`), so a comment authored *before*
    the last verdict but fetched *after* it sorts before the cutoff and isn't
    counted — the same blind spot the model has scanning the raw timeline, not
    a worse one.
    """
    cut = conn.execute(
        "SELECT MAX(ts) AS t FROM thread_events WHERE thread_id = ? AND kind = 'ai_verdict'",
        (thread_id,),
    ).fetchone()
    last_verdict_ts = cut["t"] if cut else None
    if last_verdict_ts is None:
        return None
    rows = conn.execute(
        """
        SELECT kind, payload_json FROM thread_events
         WHERE thread_id = ? AND ts > ? AND kind != 'ai_verdict'
         ORDER BY ts ASC, id ASC
        """,
        (thread_id, last_verdict_ts),
    ).fetchall()
    new_comments = 0
    new_reviews: list[dict] = []
    lifecycle: list[str] = []
    user_actions: list[str] = []
    priority_changes: list = []
    user_chats = 0
    body_edited = False
    # Per-push diff totals since the verdict — lets the model compare its prior
    # framing ("small fix") against the current scope without us hoarding
    # per-file history. Ordered oldest→newest so a series reads as a trajectory.
    code_pushes: list[dict] = []
    for r in rows:
        try:
            p = json.loads(r["payload_json"])
        except (ValueError, TypeError):
            p = {}
        k = r["kind"]
        if k == "comment":
            new_comments += 1
        elif k == "review":
            new_reviews.append({
                "author": p.get("author"),
                "state": (p.get("state") or "").lower() or None,
            })
        elif k == "lifecycle":
            if p.get("action"):
                lifecycle.append(p["action"])
        elif k == "body_edit":
            body_edited = True
        elif k == "user_action":
            if p.get("action"):
                user_actions.append(p["action"])
        elif k == "priority_change":
            priority_changes.append(p.get("to"))
        elif k == "user_chat":
            user_chats += 1
        elif k == "code":
            push = {
                "committed_at":  p.get("committed_at"),
                "additions":     p.get("additions"),
                "deletions":     p.get("deletions"),
                "changed_files": p.get("changed_files"),
            }
            code_pushes.append({pk: pv for pk, pv in push.items() if pv is not None})
    out = {
        "new_comments":     new_comments or None,
        "new_reviews":      new_reviews or None,
        "lifecycle":        lifecycle or None,
        "body_edited":      body_edited or None,
        "code_pushes":      code_pushes or None,
        "user_actions":     user_actions or None,
        "priority_changes": priority_changes or None,
        "user_chats":       user_chats or None,
    }
    return {k: v for k, v in out.items() if v is not None}


# Every timestamp the context payload carries is a full ISO-8601 UTC string
# ("2024-12-16T19:35:06Z", optionally with fractional seconds — see
# _load_timeline and the GitHub createdAt/submittedAt/updatedAt fields). The
# model is unreliable at subtracting two of these, so _annotate_ages appends a
# coarse human-readable age — "<iso> (1.4y ago)" — at every site before the
# payload is serialized. Anchored full-match only: a timestamp embedded inside a
# comment body / note / chat message is left untouched.
_ISO_UTC_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\Z")


def _humanize_age(seconds: float) -> str:
    """Magnitude, not precision: "0m" / "3h" / "15d" / "2.3mo" / "1.4y". The
    caller adds the " ago" / " from now" direction."""
    s = abs(seconds)
    if s < 3600:
        return f"{round(s / 60)}m"
    if s < 86400:
        return f"{round(s / 3600)}h"
    if s < 86400 * 60:
        return f"{round(s / 86400)}d"
    if s < 86400 * 365:
        return f"{round(s / 86400 / 30, 1)}mo"
    return f"{round(s / 86400 / 365, 1)}y"


def _annotate_ages(node, now_unix: float) -> None:
    """Recursively rewrite full ISO-8601 UTC timestamp strings in `node` (a
    dict / list, mutated in place) to "<iso> (<age> ago)". Skips `now` itself —
    the caller restores it bare afterwards."""
    items = node.items() if isinstance(node, dict) else enumerate(node) if isinstance(node, list) else None
    if items is None:
        return
    for key, val in list(items):
        if isinstance(val, (dict, list)):
            _annotate_ages(val, now_unix)
        elif isinstance(val, str) and _ISO_UTC_RE.match(val):
            ts = db.iso_to_unix(val)
            if ts is not None:
                delta = now_unix - ts
                node[key] = f"{val} ({_humanize_age(delta)} {'ago' if delta >= 0 else 'from now'})"


def _load_thread_context(conn: sqlite3.Connection, thread_id: str) -> dict | None:
    """Build the full context dict for one thread. Returns None if not found.
    Mirrors the data web._row_to_dict surfaces in the UI, so the AI sees the
    same picture the user does — just structured."""
    row = conn.execute(
        """
        SELECT id, repo, type, title, reason, html_url, link_url,
               updated_at, last_read_at, unread, ignored, action,
               is_tracked, muted_kinds,
               details_json, details_fetched_at, seen_reasons,
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
    # When this snapshot was taken — lets the prompt downweight in-flight
    # fields (pending checks, unknown mergeable_state) as it ages.
    if item and row["details_fetched_at"]:
        item["fetched_at"] = datetime.fromtimestamp(
            row["details_fetched_at"], tz=timezone.utc
        ).isoformat().replace("+00:00", "Z")

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

    muted_kinds: list[str] = []
    if row["muted_kinds"]:
        try:
            stored = set(json.loads(row["muted_kinds"]))
            muted_kinds = [k for k in github.MUTE_KINDS if k in stored]
        except (ValueError, TypeError):
            pass

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
        # Kinds the user has already silenced on this thread — lets the AI
        # avoid re-suggesting an existing mute, or propose unmuting one that's
        # become relevant. Omitted when empty.
        "muted_kinds":   muted_kinds or None,
    }
    notification = {k: v for k, v in notification.items() if v not in (None, "", False)}

    timeline = _load_timeline(conn, thread_id)
    now_dt = datetime.now(tz=timezone.utc)
    now_iso = now_dt.isoformat().replace("+00:00", "Z")

    ctx = {
        "now": now_iso,
        "notification": notification,
        "item": item,
        "activity": activity,
        "entities": entities,
        "timeline": timeline,
    }
    # Re-judgments only: an explicit, derived delta against the last verdict so
    # the model doesn't reconstruct it from the flat timeline every time. Omit
    # the key entirely on a first judgment (no prior verdict to diff against).
    changes = _changes_since_last_verdict(conn, thread_id)
    if changes is not None:
        ctx["changes_since_last_verdict"] = changes
    # Append "(<age> ago)" to every ISO timestamp so the model doesn't subtract
    # dates by hand; `now` is the reference, restored bare afterwards.
    _annotate_ages(ctx, now_dt.timestamp())
    ctx["now"] = now_iso
    return ctx


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


def _cached_verdict(conn: sqlite3.Connection, thread_id: str) -> dict | None:
    """The verdict currently cached on the row (the displayed one / the most
    recent), or None if the thread has never been judged. The inheritance
    source when a re-judgment omits a standing field, and the payload that
    `skip` re-affirms. Always a *complete* verdict — `_save_verdict` only
    ever stores fully-merged ones — so inheriting from it never leaves a gap."""
    row = conn.execute(
        "SELECT ai_verdict_json FROM notifications WHERE id = ?", (thread_id,)
    ).fetchone()
    if not row or not row["ai_verdict_json"]:
        return None
    try:
        v = json.loads(row["ai_verdict_json"])
    except (ValueError, TypeError):
        return None
    return v if isinstance(v, dict) else None


def _save_verdict(
    conn: sqlite3.Connection, thread_id: str, verdict: dict, model: str,
    *, ts: int, reclaim_priority: bool = True,
) -> None:
    # `ts` is the kick-off timestamp captured at the top of judge(), not now —
    # so a comment that arrives mid-call has a higher ts than the verdict and
    # correctly marks it outdated (auto_judge / Re-ask predicate keys on
    # `te.ts > ai_verdict_at`). See judge() for the race this closes.
    #
    # A fresh verdict normally reclaims the displayed priority: clear any
    # hand-set priority_user. The user's choice isn't lost — it survives as a
    # priority_change event in the timeline, which this judgment already saw
    # and folded into priority_score. The user can re-pin afterwards. But a
    # re-judgment that *inherited* priority_score from the prior verdict (or a
    # `skip`) isn't asserting a new priority — it's saying the old one still
    # stands — so it leaves a user pin in place (reclaim_priority=False).
    # `reasoning` is per-turn (timeline-only, AI-only): keep it out of the
    # cached row state so `_cached_verdict` never returns a stale note that
    # the skip-wholesale-copy or a future inheritance would re-stamp. The
    # caller still has the full verdict for the timeline event write.
    cached_json = json.dumps(
        {k: v for k, v in verdict.items() if k != "reasoning"},
        ensure_ascii=False,
    )
    if reclaim_priority:
        conn.execute(
            """
            UPDATE notifications
               SET ai_verdict_json = ?, ai_verdict_at = ?, ai_verdict_model = ?,
                   priority_user = NULL
             WHERE id = ?
            """,
            (cached_json, ts, model, thread_id),
        )
    else:
        conn.execute(
            """
            UPDATE notifications
               SET ai_verdict_json = ?, ai_verdict_at = ?, ai_verdict_model = ?
             WHERE id = ?
            """,
            (cached_json, ts, model, thread_id),
        )


# ---- Public API ---------------------------------------------------------

def _should_think(
    conn: sqlite3.Connection, thread_id: str, invocation_mode: str
) -> bool:
    """Whether this judgment gets an extended-thinking pass.

    On for first judgments (`summary`) and chats (`chat` — the user typed
    something that has to be weighed against surface signals). For a Re-ask
    (`re_evaluate`) there's exactly one no-thinking case: a `code` event
    landed since the verdict and nothing in `_THINKING_REQUIRED_KINDS` did —
    i.e. the user re-asked to fold in a fresh push, and the new file list /
    diff stats / last_commit are already in the prompt, nothing to deliberate
    over (and tool use can be forced, which the thinking path can't).
    Everything else thinks — including a Re-ask with *nothing* new since the
    verdict: the pill's rerun button is greyed out then, so reaching for the
    popover's "↻ Re-ask" anyway signals "I want a deeper take", which is
    exactly what the thinking pass buys. Defaults to thinking on any
    unexpected mode."""
    if invocation_mode != "re_evaluate":
        return True
    row = conn.execute(
        "SELECT ai_verdict_at FROM notifications WHERE id = ?",
        (thread_id,),
    ).fetchone()
    verdict_at = row["ai_verdict_at"] if row else None
    if not verdict_at:
        return True  # no prior verdict to anchor on — treat as a fresh pass
    placeholders = ",".join("?" for _ in _THINKING_REQUIRED_KINDS)
    has_substantive_event = conn.execute(
        f"""
        SELECT 1 FROM thread_events
         WHERE thread_id = ? AND ts > ? AND kind IN ({placeholders})
         LIMIT 1
        """,
        (thread_id, verdict_at, *_THINKING_REQUIRED_KINDS),
    ).fetchone() is not None
    if has_substantive_event:
        return True
    has_code_push = conn.execute(
        "SELECT 1 FROM thread_events "
        "WHERE thread_id = ? AND ts > ? AND kind = 'code' LIMIT 1",
        (thread_id, verdict_at),
    ).fetchone() is not None
    # No substantive event and a code push since → the cheap "fold in the
    # fresh diff" Re-ask: skip thinking. Otherwise (incl. nothing-new) → think.
    return not has_code_push


# Event kinds that count as "new context the AI hasn't seen yet" — a verdict
# made before any of these arrived is stale, and on first delivery any of
# these mean the thread is worth a first verdict. Row-state user_actions
# (read/done/mute) and `visited` are the user *responding* to a verdict, not
# new context, so they don't count. Superset of `_THINKING_REQUIRED_KINDS` —
# a code-only re-judgment is still invalidating, just runs thinking-off (the
# fresh diff is already in the prompt). Mirrored on the web side by the
# Re-ask button's enabled / outdated-border logic.
VERDICT_INVALIDATING_KINDS = (
    "comment", "review", "lifecycle", "user_chat", "body_edit", "code",
)


class AIError(RuntimeError):
    """Raised when judge() can't proceed.
    Routes catch this and surface via the existing showError HX-Trigger."""


class AIBusy(AIError):
    """Raised when judge() is already running for this thread on another
    caller (manual Re-ask vs. auto-judge race, or a double-click). Single-
    process Flask, so an in-memory lock is enough."""


# Per-thread in-flight set, guarded by a single mutex. _enter_judge returns
# True iff the caller now owns the slot and must call _exit_judge in a
# finally. Manual clicks and the auto-judge pass go through the same gate.
_IN_FLIGHT_LOCK = threading.Lock()
_IN_FLIGHT: set[str] = set()


def _enter_judge(thread_id: str) -> bool:
    with _IN_FLIGHT_LOCK:
        if thread_id in _IN_FLIGHT:
            return False
        _IN_FLIGHT.add(thread_id)
        return True


def _exit_judge(thread_id: str) -> None:
    with _IN_FLIGHT_LOCK:
        _IN_FLIGHT.discard(thread_id)


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
    in the user message; it tells the model why this judgment is firing
    (fresh thread / Re-ask / the user sent a chat) — not what the output
    should look like, which is the same across modes (see ai_system_prompt.md
    §Invocation modes). Defaults to `summary` for callers that don't supply it.

    On a re-judgment (a prior verdict exists) the model may omit
    `disposition` / `priority_score` / `description` from `judge_thread` — the
    merge fills them from `prior_verdict` — and in `re_evaluate` mode may call
    `skip` instead, which re-affirms the prior verdict unchanged. An inherited
    `priority_score` (and `skip`) won't clear a hand-set `priority_user`; a
    freshly-supplied one will. See ai_system_prompt.md §Output fields.

    Concurrency: a per-thread in-memory lock (`_IN_FLIGHT`) serializes calls
    for the same thread — a manual Re-ask racing the auto-judge pass, or a
    double-click, gets `AIBusy` on the second caller rather than double-spending
    the API. The kick-off timestamp `ts_start` is captured before the call and
    used for both `ai_verdict_at` and the `ai_verdict` event's `ts`, so any
    activity that lands while the model is thinking sorts *after* the verdict
    and correctly marks it outdated."""
    # Capture early so activity that arrives mid-call sorts after the verdict.
    ts_start = int(time.time())
    if not _enter_judge(thread_id):
        raise AIBusy(f"judgment already in flight for {thread_id}")
    try:
        # Block until the row's enrichment is up to date — judging against a
        # half-built thread_events timeline would feed the model stale context
        # (missing the comments / reviews / lifecycle / code events that
        # _enrich is about to fan out). 60s is the hard ceiling; on a real
        # enrichment failure the manual click surfaces this as a toast and
        # the auto-judge worker logs + skips.
        if not github.wait_until_enriched(conn, thread_id, timeout=60.0):
            raise AIError(f"enrichment not complete for {thread_id}; try again shortly")
        return _judge_locked(
            thread_id, conn, ts_start=ts_start,
            user_login=user_login, user_teams=user_teams,
            invocation_mode=invocation_mode,
        )
    finally:
        _exit_judge(thread_id)


def _judge_locked(
    thread_id: str,
    conn: sqlite3.Connection,
    *,
    ts_start: int,
    user_login: str | None,
    user_teams,
    invocation_mode: str,
) -> dict:
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

    # The prior verdict (if any) is both the fallback for omitted standing
    # fields on a re-judgment and the payload `skip` re-affirms. `skip` is
    # offered only on a plain Re-ask with a verdict to fall back on — never
    # right after the user has spoken (chat) or on a first pass.
    prior_verdict = _cached_verdict(conn, thread_id)
    has_prior = prior_verdict is not None
    offer_skip = has_prior and invocation_mode == "re_evaluate"

    # Tool schema is per-thread: subscription_changes only offers the kinds
    # that fire on this type (right mute/unmute direction), and a re-judgment
    # drops the inheritable standing fields from `required` (see _build_tool_def).
    tools = [
        _build_tool_def(
            ctx["notification"]["type"], ctx["notification"].get("muted_kinds"),
            has_prior=has_prior,
        )
    ]
    if offer_skip:
        tools.append(SKIP_TOOL_DEF)

    system_prompt = _read_system_prompt()
    identity = _identity_block(user_login, user_teams)
    if identity:
        system_prompt = f"{identity}\n\n{system_prompt}"
    prefs = _read_preferences()
    user_msg = _build_user_message(ctx)

    # Extended thinking + a *forced* tool_choice are mutually exclusive on
    # the API ("Thinking may not be enabled when tool_choice forces tool
    # use"), so:
    #   thinking on  → tool_choice left to {"type": "auto"}; the only tools
    #     defined are the verdict tools and the system prompt insists, which
    #     reliably gets Claude to call one (AIError below if not — the route
    #     shows a red toast, nothing is cached).
    #   thinking off → {"type": "any"} forces *a* tool call but leaves Claude
    #     the judge_thread-vs-skip choice — this is the boring re-judgment
    #     path, exactly where `skip` is often the right call.
    # _should_think turns thinking off for code-push / metadata-churn
    # re-judgments — see its docstring.
    think = _should_think(conn, thread_id, invocation_mode)
    thinking_cfg: dict = (
        {"type": "enabled", "budget_tokens": DEFAULT_THINKING_BUDGET}
        if think else {"type": "disabled"}
    )
    tool_choice: dict | None = None if think else {"type": "any"}

    request_log = {
        "model": model,
        "max_tokens": DEFAULT_MAX_TOKENS,
        "thinking": thinking_cfg,
        **({"tool_choice": tool_choice} if tool_choice else {}),
        # System and user content are logged in full so we can replay the
        # exact prompt later when tuning. Static content (system prompt,
        # prefs) is repeated across rows; that's the price of being able
        # to audit individual calls.
        "system": [system_prompt, prefs],
        "user_message": user_msg,
        "tools": tools,
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

    create_kwargs: dict = dict(
        model=model,
        max_tokens=DEFAULT_MAX_TOKENS,
        thinking=thinking_cfg,
        system=[
            {"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": prefs, "cache_control": {"type": "ephemeral"}},
        ],
        tools=tools,
        messages=[{"role": "user", "content": user_msg}],
    )
    if tool_choice:
        create_kwargs["tool_choice"] = tool_choice

    try:
        response = client.messages.create(**create_kwargs)
    except anthropic.APIError as e:
        msg = f"{type(e).__name__}: {e}"
        _log_call(
            conn, thread_id=thread_id, model=model,
            request=request_log, response=None, usage=None, cost_usd=0.0,
            error=msg, status="error",
        )
        raise AIError(msg) from e

    # Find the verdict tool_use block — judge_thread or skip. With tool_choice
    # {"type": "any"} (thinking off) there's exactly one; with thinking on the
    # system prompt + the fact that only verdict tools are defined gets Claude
    # to call one — defend against drift either way (no block ⇒ AIError).
    tool_use = next(
        (b for b in response.content if getattr(b, "type", None) == "tool_use"
         and getattr(b, "name", None) in ("judge_thread", "skip")),
        None,
    )
    response_dict = response.model_dump() if hasattr(response, "model_dump") else None
    usage = (response_dict or {}).get("usage") or {}
    cost = _estimate_cost(model, usage)

    def _fail(msg: str) -> AIError:
        _log_call(
            conn, thread_id=thread_id, model=model,
            request=request_log, response=response_dict, usage=usage,
            cost_usd=cost, error=msg, status="error",
        )
        return AIError(msg)

    if tool_use is None:
        raise _fail("Model did not call judge_thread or skip")

    is_skip = getattr(tool_use, "name", None) == "skip"
    if is_skip and not has_prior:
        # skip is only ever *offered* with a prior verdict; if the model
        # calls it anyway there's nothing to re-affirm.
        raise _fail("Model called skip but there is no prior verdict to re-affirm")

    if is_skip:
        # Re-affirm the cached verdict verbatim. `skip` asserts nothing
        # changed, so it doesn't reclaim a hand-set priority. `prior_verdict`
        # is always a complete, `model`-free dict (see _cached_verdict) and
        # carries no `reasoning` (stripped at row-write — per-turn field).
        # A skip *may* still carry a fresh reasoning note; layer it on so it
        # lands in the timeline event for future-you to read.
        verdict = dict(prior_verdict)
        raw = dict(tool_use.input or {})
        if raw.get("reasoning"):
            verdict["reasoning"] = raw["reasoning"]
        reclaim_priority = False
    else:
        raw = dict(tool_use.input or {})
        verdict = dict(raw)
        # Fill omitted standing fields from the prior verdict — the schema
        # only drops them from `required` when `has_prior`, so this is the
        # AI's deliberate "keep my last value". `snooze_days` rides with
        # `disposition`: inherited together if `disposition` is inherited
        # (incl. either snooze flavour); if `disposition` is fresh, the model
        # supplies `snooze_days` alongside.
        if has_prior:
            for k in ("disposition", "priority_score", "description", "snooze_days"):
                if k not in verdict and k in prior_verdict:
                    verdict[k] = prior_verdict[k]
        # `set_tracked` is per-turn, not standing: omitted means "no change this
        # turn". Normalize to 'leave' so the stored verdict stays complete (the
        # cached-verdict / purple-ring code reads this field unconditionally).
        verdict.setdefault("set_tracked", "leave")
        # A fresh priority_score reclaims the displayed priority; an inherited
        # one leaves a user pin alone (see _save_verdict).
        reclaim_priority = "priority_score" in raw

    missing = {"disposition", "priority_score", "description"} - verdict.keys()
    if missing:
        raise _fail(f"Verdict missing fields: {sorted(missing)}")

    _save_verdict(
        conn, thread_id, verdict, model,
        ts=ts_start, reclaim_priority=reclaim_priority,
    )
    ai_call_id = _log_call(
        conn, thread_id=thread_id, model=model,
        request=request_log, response=response_dict, usage=usage,
        cost_usd=cost, error=None, status="ok",
    )
    # Append the (possibly re-affirmed) verdict to the per-thread timeline.
    # external_id joins back to ai_calls so the full request / response is one
    # query away; `model` rides in the payload so timeline-render can show
    # which model produced it. A `skip` lands here as another ai_verdict event
    # with the prior payload — the timeline collapses superseded verdicts, so
    # it just advances the "AI last looked here" mark. `ts=ts_start` (not now)
    # so any activity that arrived while the model was thinking sorts after
    # this row — see the judge() docstring.
    db.write_thread_event(
        conn,
        thread_id=thread_id,
        ts=ts_start,
        kind="ai_verdict",
        source="ai",
        external_id=str(ai_call_id),
        payload={**verdict, "model": model},
    )
    return verdict


# Per-pass safety cap. The daily $-cap is the real bound, but a fat eligible
# list on first-enable could otherwise block the poll thread for minutes —
# snooze wake and SSE fingerprint checks run in the same loop. Splitting
# across passes keeps each iteration quick; the next pass picks up whatever
# this one didn't get to.
_AUTO_JUDGE_PER_PASS = 20


def auto_judge_eligible(
    conn: sqlite3.Connection,
    *,
    user_login: str | None = None,
    user_teams=None,
) -> int:
    """Background pass: judge rows where the Re-assess button would be enabled
    (stale or absent verdict + new invalidating activity), subject to the
    `ai_auto_judge_since` watermark and the app's noise suppressors. Returns
    the number of verdicts written.

    Gated by `triage_mode='ai'` + `ai_auto_judge=True`; either off → no-op.
    The watermark — set by `web._set_ai_auto_judge` on False→True — makes
    the toggle prospective: existing un-judged history doesn't get swept on
    enable. Two additional suppressors over Re-assess: rows currently
    throttled (`throttle_until > now`) are skipped, and invalidating events
    are only counted if their ts is past the most recent `absorbed`
    user_action — so a muted-only re-delivery (`_apply_mute_filter`) doesn't
    trigger a judgment, but a subsequent directed-reason delivery does.
    Done / muted / snoozed / synthetic rows are *not* specially excluded:
    Re-assess works on them, and the suppressors above plus the watermark
    handle the cost-sensitive cases naturally.

    Each `judge()` call is the same code path as a manual click; the daily
    cap is enforced inside `judge()`. To avoid log-spamming cap_exceeded
    rows we pre-check `_spent_today` once and bail before the loop, and
    break the loop on the first cap_exceeded from `judge()`.

    Safe to call from the poll thread — uses the connection it was passed,
    no Flask request context required."""
    if settings.get("triage_mode") != "ai":
        return 0
    if not settings.get("ai_auto_judge"):
        return 0
    raw_watermark = db.get_meta(conn, "ai_auto_judge_since")
    if not raw_watermark:
        return 0
    try:
        watermark = int(raw_watermark)
    except (TypeError, ValueError):
        log.warning("auto-judge: invalid ai_auto_judge_since=%r; skipping pass", raw_watermark)
        return 0

    cap = _daily_cap()
    if _spent_today(conn) >= cap:
        return 0

    now = int(time.time())
    kind_placeholders = ",".join("?" * len(VERDICT_INVALIDATING_KINDS))
    # The inner EXISTS mirrors Re-assess's `ai_uptodate` predicate (event of
    # an invalidating kind past `ai_verdict_at`) and adds two suppressors:
    # the absorb-marker check (the event must be newer than the latest
    # `absorbed` user_action, so muted-kinds re-deliveries don't qualify)
    # and the watermark (prospective-only on toggle). Throttle is a
    # row-level skip; muted/done/snoozed/synthetic intentionally aren't —
    # they naturally don't accumulate post-verdict invalidating activity
    # while in-state, and the absorb-marker check handles the one edge
    # case (`_apply_mute_filter` keeping a Done row in Done). Newest
    # bumped-row first so top-of-inbox gets verdicts before the cap trips.
    rows = conn.execute(
        f"""
        SELECT n.id AS id
          FROM notifications n
         WHERE (n.throttle_until IS NULL OR n.throttle_until <= ?)
           AND EXISTS (
               SELECT 1 FROM thread_events te
                WHERE te.thread_id = n.id
                  AND te.kind IN ({kind_placeholders})
                  AND te.ts > ?
                  AND te.ts > COALESCE(n.ai_verdict_at, 0)
                  AND te.ts > COALESCE((
                      SELECT MAX(ab.ts) FROM thread_events ab
                       WHERE ab.thread_id = n.id
                         AND ab.kind = 'user_action'
                         AND json_extract(ab.payload_json, '$.action') = 'absorbed'
                  ), 0)
           )
         ORDER BY n.effective_updated_at DESC, n.updated_at DESC
         LIMIT ?
        """,
        (now, *VERDICT_INVALIDATING_KINDS, watermark, _AUTO_JUDGE_PER_PASS),
    ).fetchall()
    if not rows:
        return 0
    if len(rows) >= _AUTO_JUDGE_PER_PASS:
        # Hitting the cap is unusual in steady state — flag it so we notice
        # if focus events ever bunch up eligible rows (deploy after a long
        # downtime, a watermark gone stale, etc.).
        log.info("auto-judge: eligibility scan hit per-pass cap of %d", _AUTO_JUDGE_PER_PASS)

    judged = 0
    for r in rows:
        thread_id = r["id"]
        # Re-check the predicate right before the call: a manual Ask AI may
        # have judged this thread between the SELECT and now, and we don't
        # want to double-spend. (judge() also has a per-thread in-flight lock
        # — AIBusy — that catches the same race when the manual call is still
        # in flight; this predicate catches it after it has already completed.)
        current = conn.execute(
            f"""
            SELECT 1 FROM notifications n
             WHERE n.id = ?
               AND (n.throttle_until IS NULL OR n.throttle_until <= ?)
               AND EXISTS (
                   SELECT 1 FROM thread_events te
                    WHERE te.thread_id = n.id
                      AND te.kind IN ({kind_placeholders})
                      AND te.ts > ?
                      AND te.ts > COALESCE(n.ai_verdict_at, 0)
                      AND te.ts > COALESCE((
                          SELECT MAX(ab.ts) FROM thread_events ab
                           WHERE ab.thread_id = n.id
                             AND ab.kind = 'user_action'
                             AND json_extract(ab.payload_json, '$.action') = 'absorbed'
                      ), 0)
               )
             LIMIT 1
            """,
            (thread_id, now, *VERDICT_INVALIDATING_KINDS, watermark),
        ).fetchone()
        if not current:
            continue
        has_prior = conn.execute(
            "SELECT ai_verdict_at FROM notifications WHERE id = ?",
            (thread_id,),
        ).fetchone()
        mode = "re_evaluate" if (has_prior and has_prior["ai_verdict_at"]) else "summary"
        try:
            judge(
                thread_id, conn,
                user_login=user_login, user_teams=user_teams,
                invocation_mode=mode,
            )
            conn.commit()
            judged += 1
        except AIBusy:
            # Manual click is mid-call on this same thread — skip silently and
            # let the in-flight call write its verdict; this pass will pick up
            # any remaining staleness on the next tick.
            log.debug("auto-judge: %s busy (manual call in flight), skipping", thread_id)
        except AIError as e:
            # cap_exceeded is the only AIError that should halt the pass —
            # one row failing for any other reason (transient API error,
            # missing context) shouldn't starve the rest. judge() already
            # logged the call; we just decide whether to keep going.
            if "cap" in str(e).lower():
                log.info("auto-judge: daily cap reached after %d verdict(s)", judged)
                break
            log.warning("auto-judge: %s failed: %s", thread_id, e)
        except Exception:
            log.exception("auto-judge: %s crashed", thread_id)
    return judged
