# GitHub notification triage

A local Flask app that turns GitHub's notification firehose into a "you should look at this" view. Single user, runs as `source .venv/bin/activate && python -m app run` and serves on `localhost:5734`. Auth via `gh auth token` (with `GITHUB_TOKEN` env override).

The point is *triage* — surface what needs attention, quiet what doesn't — not a fancier inbox. AI integration carries most of the noise reduction; the local infrastructure is the substrate that gives it useful signal to work with. v1 is event-sourced — see "AI v1" below.

## Stack

Flask + Jinja + HTMX + Pico CSS + SQLite. Static assets vendored under `static/vendor/`. Python 3.11, stdlib `venv` + `pip`, no lockfile. Schema migrates via a hand-rolled version ladder in `app/db.py` — add a new `SCHEMA_VN` rather than editing existing ones.

## Cross-cutting principles

**GitHub is the source of truth.** The local DB is an augmented cache: stores its own state (favorites, notes, baselines) but doesn't fight GitHub on shared state (read/unread, ignored). The app should remain useful as a "fancier inbox" even with no AI active.

**Indicators persist across user actions.** "+N new comments", `Mentioned`, review-state newness — they stay visible after the user takes an action so the user can see what they just handled. Reset only when actual notification activity shifts them. This shapes the data model: baselines are captured lazily during enrichment, never touched by action handlers. See comments around `_apply_action` and `_enrich`.

**Two-fetch poll.** Each poll runs an unread fetch (default `/notifications` + reconciliation) and a since-fetch (`?all=true&since=<bookmark>`). They catch disjoint cases — see the docstrings in `app/github.py`. Both bounded; deeper history goes through Backfill.

## AI v1 (event-sourced)

User-triggered, approve-gated per-thread judgment with **episodic memory**. Each thread carries a chronological event log (`thread_events` table) — every comment, review, AI verdict, user action on a verdict, and free-text user message. The AI sees the full timeline on every judgment, so it reasons about *what's changed since last time* rather than re-classifying a thread from scratch.

Lives in the Relevance column, behind a brain-icon mode toggle in the column header. Manual mode shows the rule-based status pill + prose subhead; AI mode replaces both — per-row **Ask AI** button when no verdict cached, split-pill (popover left + ✓ approve right) when one exists, plus AI-selected signal pills below.

**Inputs:**

- `config/preferences.md` — free-text user prefs (interests, important repos / people, noise patterns). Loaded into the cached system block. `config/preferences.example.md` ships as a template.
- `app/ai_system_prompt.md` — shipped instructions (cost asymmetry, brevity rules, output-field semantics, signal vocabulary, **timeline interpretation**). The "do not restate row-visible facts" rule is load-bearing; the "user_chat is authoritative for this thread" rule is the v1 addition.

**Verdict shape (single tool call, forced via the prompt):**

```python
judge_thread({
    action_now:       "none" | "mark_read" | "mute" | "archive",
    set_tracked:      "track" | "untrack" | "leave",
    priority_score:   float ∈ [0, 1],   # bucketed to low/normal/high for color
    relevant_signals: list[str],         # 0–3 keys from a controlled vocabulary
    description:      str,               # what the row doesn't already show
})
```

`relevant_signals` vocabulary lives in `app/ai.py:SIGNAL_VOCAB`; the display label / CSS class for each key is in `app/web.py:_SIGNAL_LABELS`. Adding a key requires touching both. `description` lands in `notifications.note_ai` on approve; the verdict cache (`notifications.ai_verdict_*`) is cleared on approve / dismiss.

**`thread_events` table (SCHEMA_V20):** chronological per-thread log keyed by `(thread_id, kind, external_id)` where present. The unique partial index makes re-fetched comments / reviews idempotent — same GitHub `databaseId` UPDATEs the payload (catches body edits) instead of appending. Event kinds:

- `comment` / `review` — GitHub-side activity, source `github`. Written by `_enrich` in `app/github.py`.
- `ai_verdict` — the AI's proposal, source `ai`. external_id joins back to `ai_calls.id` so the full request / response is one query away. Written by `ai.judge` on success.
- `user_action` — read / mute / done / kept_unread / unmuted / approve_verdict / dismiss_verdict. source is `user` or `ai` (AI-applied actions get source=ai). Written by `_apply_action` (manual) and `_set_state` / `apply_verdict` / `dismiss_verdict` (AI flow).
- `user_chat` — free-text per-thread message, source `user`. Written by `POST /ai/<id>/chat`. NULL external_id; each save is its own event (no dedup).

**Popover (`templates/_row.html`):** chat-style conversation log. AI verdicts and user chat messages render as full chat bubbles (purple-tinted on the right for AI, blue-tinted on the left for the user); comment / review / user_action events collapse to one-line muted entries. A "⚠ N new since judgment" badge surfaces whenever GitHub events or user chats have landed after the cached verdict — Approve still works (warn, don't block; Re-ask is the user's call). Composer at the bottom posts to `/ai/<id>/chat`; `hx-on::after-request` clears the textarea on success so chat is appended, not overwritten.

**No autonomous re-judgment in v1** — explicit Ask AI / Re-ask only. Per-thread `policy` field intentionally absent. Add when auto re-judging ships; the natural trigger is "N new events since latest ai_verdict" exceeding a threshold.

**Storage:** every API call writes a row to `ai_calls` (full request + response, token breakdown, estimated cost, status). Useful for prompt tuning and the soft daily cap (`AI_DAILY_CAP_USD`, default $2). Past verdicts also live as `ai_verdict` events on `thread_events` — joined via `external_id`.

**Default model:** Haiku 4.5. Configurable via `AI_MODEL`. The system prompt + preferences sit at ~3k tokens after the timeline-interpretation section — still below Haiku's 4096-token cache minimum, so `cache_control` markers don't fire today; they're forward-compatible once preferences grow.

**Approve-button no-op detection:** `_approve_label` in `app/web.py` drops parts of the verdict that are already true on the row (mark_read on a read row, track on a tracked row, …). When everything's a no-op, the button is `disabled` and tells the user to use Dismiss. Both the row-level approve (✓ on the split-pill) and the popover Approve button respect this.

**Mode toggle plumbing:** `triage_mode` is a persisted user setting, not a filter — stored in the `meta` table (`_get_triage_mode` / `_set_triage_mode` in `app/web.py`). The brain button in the column header posts to `/settings/triage_mode`, which flips the value and returns the re-rendered table. Per-row HTMX swaps read the mode server-side, so it doesn't need to ride the request.

**History compression** (deferred): timelines are uncompressed in v1. When threads grow long enough to matter, the hook is `kind=ai_recap` events that summarize older history; the AI reads recaps in lieu of the events they replaced.

## How the user works

- One coherent change per commit. Brief message, *why* over *what*. Co-authored by Claude Opus.
- Pushes back on suggestions; expects pushback in return. "Clean wins" is the recurring frame.
- Boring stable tech preferred. No feature-flag / fallback proliferation.
- Often probes edge cases ("what if I do this on mobile?") before settling UX.
