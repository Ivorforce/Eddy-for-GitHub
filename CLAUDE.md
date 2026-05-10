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

User-triggered, **advisory** per-thread judgment with **episodic memory**. Each thread carries a chronological event log (`thread_events` table) — every comment, review, AI verdict, row-state user action, and free-text user message. The AI sees the full timeline on every judgment, so it reasons about *what's changed since last time* rather than re-classifying a thread from scratch.

Lives in the Relevance column, behind a brain-icon mode toggle in the column header. Manual mode shows the rule-based status pill + prose subhead; AI mode replaces both — per-row **Ask AI** button when no verdict cached, an informational pill once one exists (click to open the popover), plus AI-selected signal pills below.

Verdicts are advisory only: the pill / signal pills / priority color shape *display*, but no row state is auto-applied. The user takes their own row actions (visit, mark read, mute, archive, track). Those land as `user_action` events in the timeline; the next judgment compares them against the prior verdict and recalibrates ("I suggested ignore, they visited → I underestimated interest").

**Inputs:**

- `config/preferences.md` — free-text user prefs (interests, important repos / people, noise patterns). Loaded into the cached system block. `config/preferences.example.md` ships as a template.
- `app/ai_system_prompt.md` — shipped instructions (cost asymmetry, brevity rules, output-field semantics, signal vocabulary, **timeline interpretation**). The "do not restate row-visible facts" rule is load-bearing; "user_chat is authoritative for this thread" and "user_action after a verdict is calibration feedback" are the v1 reading-rules.

**Verdict shape (single tool call, forced via the prompt):**

```python
judge_thread({
    action_now:       "look" | "ignore" | "mute" | "archive",
    set_tracked:      "track" | "untrack" | "leave",
    priority_score:   float ∈ [0, 1],   # bucketed to low/normal/high for color
    relevant_signals: list[str],         # 0–3 keys from a controlled vocabulary
    description:      str,               # what the row doesn't already show
})
```

`action_now` is a suggestion for what the user should do — `look` (open the link), `ignore` (mark read without engaging), `mute` (silence further updates), `archive` (nothing left to do). Nothing auto-applies. `relevant_signals` vocabulary lives in `app/ai.py:SIGNAL_VOCAB`; the display label / CSS class for each key is in `app/web.py:_SIGNAL_LABELS`. Adding a key requires touching both. The verdict cache (`notifications.ai_verdict_*`) is never auto-cleared — Re-ask overwrites it (and `_save_verdict` keeps the prior verdict in `thread_events` for the next judgment to see).

**`thread_events` table (SCHEMA_V20):** chronological per-thread log keyed by `(thread_id, kind, external_id)` where present. The unique partial index makes re-fetched comments / reviews idempotent — same GitHub `databaseId` UPDATEs the payload (catches body edits) instead of appending. Event kinds:

- `comment` / `review` / `lifecycle` — GitHub-side activity, source `github`. Written by `_enrich` in `app/github.py`.
- `ai_verdict` — the AI's verdict, source `ai`. external_id joins back to `ai_calls.id` so the full request / response is one query away. Written by `ai.judge` on success.
- `user_action` — `visited` / `read` / `read_on_github` / `muted` / `done` / `kept_unread` / `unmuted`. source is `user` (clicked in our app) or `github` (observed remotely, e.g. mark-read on github.com). Written by `_apply_action`.
- `user_chat` — free-text per-thread message, source `user`. Written by `POST /ai/<id>/chat`. NULL external_id; each save is its own event (no dedup).

**Popover (`templates/_row.html`):** chat-style conversation log. AI verdicts and user chat messages render as full chat bubbles (purple-tinted on the right for AI, blue-tinted on the left for the user); comment / review / user_action events collapse to one-line muted entries. A "⚠ N new since judgment" badge surfaces whenever GitHub events or user chats have landed after the cached verdict (row-state user actions don't count — they *are* the user's response). Composer at the bottom posts to `/ai/<id>/chat`; `hx-on::after-request` clears the textarea on success so chat is appended, not overwritten.

**No autonomous re-judgment in v1** — explicit Ask AI / Re-ask only. Per-thread `policy` field intentionally absent. Add when auto re-judging ships; the natural trigger is "N new events since latest ai_verdict" exceeding a threshold.

**Storage:** every API call writes a row to `ai_calls` (full request + response, token breakdown, estimated cost, status). Useful for prompt tuning and the soft daily cap (`AI_DAILY_CAP_USD`, default $2). Past verdicts also live as `ai_verdict` events on `thread_events` — joined via `external_id`.

**Default model:** Haiku 4.5. Configurable via `AI_MODEL`. The system prompt + preferences sit at ~3k tokens after the timeline-interpretation section — still below Haiku's 4096-token cache minimum, so `cache_control` markers don't fire today; they're forward-compatible once preferences grow.

**Mode toggle plumbing:** `triage_mode` is a persisted user setting, not a filter — stored in the `meta` table (`_get_triage_mode` / `_set_triage_mode` in `app/web.py`). The brain button in the column header posts to `/settings/triage_mode`, which flips the value and returns the re-rendered table. Per-row HTMX swaps read the mode server-side, so it doesn't need to ride the request.

**History compression** (deferred): timelines are uncompressed in v1. When threads grow long enough to matter, the hook is `kind=ai_recap` events that summarize older history; the AI reads recaps in lieu of the events they replaced.

## How the user works

- One coherent change per commit. Brief message, *why* over *what*. Co-authored by Claude Opus.
- Pushes back on suggestions; expects pushback in return. "Clean wins" is the recurring frame.
- Boring stable tech preferred. No feature-flag / fallback proliferation.
- Often probes edge cases ("what if I do this on mobile?") before settling UX.
