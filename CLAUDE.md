# GitHub notification triage

A local Flask app that turns GitHub's notification firehose into a "you should look at this" view. Single user, runs as `source .venv/bin/activate && python -m app run` and serves on `localhost:5734`. Auth via `gh auth token` (with `GITHUB_TOKEN` env override).

The point is *triage* — surface what needs attention, quiet what doesn't — not a fancier inbox. AI integration carries most of the noise reduction; the local infrastructure is the substrate that gives it useful signal to work with. v1 is event-sourced — see "AI v1" below.

## Stack

Flask + Jinja + HTMX + Pico CSS + SQLite. Static assets vendored under `static/vendor/`; UI icons are Octicons (vendored there, inlined as `<path>` data in templates with `fill="currentColor"` — the funnel brand mark in `static/icon.svg` / the header is ours). Bundled/depended third-party licenses are reproduced in `THIRD_PARTY_LICENSES.md`. Python 3.11, stdlib `venv` + `pip`, no lockfile. Schema migrates via a hand-rolled version ladder in `app/db.py` — add a new `SCHEMA_VN` rather than editing existing ones.

## Cross-cutting principles

**GitHub is the source of truth.** The local DB is an augmented cache: stores its own state (favorites, notes, baselines) but doesn't fight GitHub on shared state (read/unread, ignored). The app should remain useful as a "fancier inbox" even with no AI active.

**Indicators persist across user actions.** "+N new comments", `Mentioned`, review-state newness — they stay visible after the user takes an action so the user can see what they just handled. Reset only when actual notification activity shifts them. This shapes the data model: baselines are captured lazily during enrichment, never touched by action handlers. See comments around `_apply_action` and `_enrich`.

**Two-fetch poll.** Each poll runs an unread fetch (default `/notifications` + reconciliation) and a since-fetch (`?all=true&since=<bookmark>`). They catch disjoint cases — see the docstrings in `app/github.py`. Both bounded; deeper history goes through Backfill.

**Archive is soft; Mute is permanent.** Done (`action='done'`, hidden by default) resurfaces — `action`→NULL — the next time a poll re-delivers the notification, which GitHub only does on new activity (mirrors github.com's Done auto-reset). Mute (`action='done'` + `ignored=1`) doesn't: the unsubscribe stops delivery. AI re-judgment never resurfaces a row — only GitHub activity (or clicking Done again). Lives in `_upsert`; logs an `unarchived` user_action.

**Snooze hides with a wake timer.** `action='snoozed'` + `notifications.snooze_until`; archived on GitHub like Done, resurfaced unread by `poll._wake_snoozed` when the timer passes — or sooner on new GitHub activity, since plain snooze stays subscribed. The **quiet** variant (popover checkbox → `&quiet=1` on `POST /set/<id>/snooze`) *also* unsubscribes (`ignored=1`) for the duration — for a busy thread you want a periodic digest of, not a live feed — so nothing resurfaces it early; the wake re-subscribes. `ignored` is the only thing that distinguishes the two flavours. See `set_snooze` / `_wake_snoozed`.

**Per-thread kind filter (user-controlled, no AI).** GitHub thread subscriptions are all-or-nothing; `notifications.muted_kinds` lets the user mute individual activity types (`comment` / `review` / `code`; `lifecycle` — merge/close/reopen/answered — always notifies and isn't mutable, shown as a disabled menu row) on a thread via the ▾ dropdown on the Mute segment. The mutable set and which type produces which live in `github.MUTE_KINDS` / `MUTE_KINDS_BY_TYPE`. A re-delivery whose new activity is *entirely* muted kinds gets **absorbed** — `github._apply_mute_filter` re-applies the prior row state, advances baselines past the activity, and freezes `effective_updated_at` (the local sort key) so the row keeps its slot. Directed reasons (`mention` / `team_mention` / `assign` / `review_requested`) override the mute. `muted_kinds` / `baseline_head_oid` / `effective_updated_at` are in the "not touched by `_apply_action`" club. See the `SCHEMA_V25` comment in `app/db.py` and `_apply_mute_filter` for the mechanics.

**Bystander throttle (global, no per-thread UI).** Bursts of `comment` / `code` activity on a thread the user is only watching surface once, then a 30 min window (`notifications.throttle_until`) freezes `effective_updated_at` so the row keeps its slot; when it expires, the next activity (or `_release_throttled`) bumps once with the accumulated "+N new comments" behind it. Exempt: directed reasons, involved reasons (`author` / `comment` / `manual` / `your_activity`), tracked threads, archived/snoozed rows, and any delivery that includes `review` or `lifecycle`. Toggle is the meta key `quiet_bystanders` (default on; `POST /settings/quiet_bystanders`). See `github._apply_throttle`.

## AI v1 (event-sourced)

User-triggered, **advisory** per-thread judgment with **episodic memory**. Each thread carries a chronological event log (`thread_events` table) — every comment, review, AI verdict, row-state user action, and free-text user message. The AI sees the full timeline on every judgment, so it reasons about *what's changed since last time* rather than re-classifying a thread from scratch.

Lives in the Relevance column — two rows, same shape in both modes (toolbar Options ▸ "AI triage mode" toggles): a **pill group** on top, the **priority picker** below (`PRIORITY_UI` → `POST /set/<id>/priority`, logs a `priority_change` event; unpinned cell falls back to the AI verdict's score, or neutral in manual mode). The pill's left half opens the timeline popover — reachable even with no verdict. Manual mode left = the rule-based activity/state summary (`_thread_pill`: headline + ellipsized subtext, full breakdown in the tooltip; baseline-diff–derived, never re-derived from `thread_events`), right = a note icon → per-thread note popover (`POST /note/<id>`, amber when a note exists). AI mode left = the AI's take (`look`/`ignore`/`mute`/`archive` + track hint, or `unassessed`; `description` in the tooltip), right = the assess/re-assess trigger (`POST /ai/<id>/judge`) — greyed + disabled when `ai_uptodate` (a verdict exists, details not re-enriched since, no `comment`/`review`/`lifecycle`/`user_chat` since — see `_VERDICT_INVALIDATING_KINDS`, `_attach_timeline`), else purple. An outdated verdict gets a dashed pill border.

Verdicts are advisory only: the pill / priority color shape *display*, but no row state is auto-applied. The user takes their own row actions (visit, mark read, mute, archive, track). Those land as `user_action` events in the timeline; the next judgment compares them against the prior verdict and recalibrates ("I suggested ignore, they visited → I underestimated interest").

**Inputs:**

- `config/preferences.md` — free-text user prefs (interests, important repos / people, noise patterns). Loaded into the cached system block. `config/preferences.example.md` ships as a template.
- `app/ai_system_prompt.md` — shipped instructions (cost asymmetry, brevity rules, output-field semantics, **timeline interpretation**). The "do not restate row-visible facts" rule is load-bearing; "user_chat is authoritative for this thread" and "user_action after a verdict is calibration feedback" are the v1 reading-rules.

**Verdict shape (single tool call, forced via the prompt):**

```python
judge_thread({
    action_now:           "look" | "ignore" | "mute" | "archive" | "snooze",
    snooze_days:          int,                  # only with action_now == "snooze"
    set_tracked:          "track" | "untrack" | "leave",
    priority_score:       float ∈ [0, 1],       # bucketed to low/normal/high for color
    subscription_changes: ["mute_<kind>" | "unmute_<kind>", ...],  # usually []; per-thread mute_kinds tweaks
    description:          str,                  # what the row doesn't already show — the standing take
    reply:               str,                  # optional; a direct reply to a user_chat — omitted when there's nothing to say
})
```

`action_now` is a suggestion for what the user should do — `look` (open the link), `ignore` (mark read without engaging), `mute` (silence further updates), `archive` (nothing left to do), `snooze` (hide until ~`snooze_days` out). `subscription_changes` is a forward-looking, advisory delta on the thread's `muted_kinds` (which the AI now sees in its input); the per-thread tool schema only offers the kinds that fire on that type, in the right mute/unmute direction. `reply` is the one non-standing field — a conversational answer when a `user_chat` asked something; the prompt tells the AI to omit it rather than emit filler. Nothing auto-applies. The verdict cache (`notifications.ai_verdict_*`) is never auto-cleared — Re-ask overwrites it (and `_save_verdict` keeps the prior verdict in `thread_events` for the next judgment to see).

In **AI mode**, the verdict's suggestions show as purple inset rings on the matching Actions-column controls — `action_now` → the Ignore / Done / Mute / Snooze button, `set_tracked` → the Track button, an unapplied `subscription_changes` → the ▾ caret (plus the affected option(s) inside its menu, plus an "✦ apply suggestion" row that bulk-applies via `POST /ai/<id>/apply-mute-suggestion`). Every ring clears once that suggestion is in effect, so the purple only ever means "act on this", never "undo this" or "this is special" (`look` has no button, so it rings nothing). Computed in `_row_to_dict` as `action_pending` / `track_pending` / the `pending_*` lists on the verdict dict; manual mode shows none of it.

**`thread_events` table (SCHEMA_V20):** chronological per-thread log keyed by `(thread_id, kind, external_id)` where present. The unique partial index makes re-fetched comments / reviews idempotent — same GitHub `databaseId` UPDATEs the payload (catches body edits) instead of appending. Event kinds:

- `comment` / `review` / `lifecycle` — GitHub-side activity, source `github`. Written by `_enrich` in `app/github.py`.
- `ai_verdict` — the AI's verdict, source `ai`. external_id joins back to `ai_calls.id` so the full request / response is one query away. Written by `ai.judge` on success.
- `user_action` — `visited` / `read` / `read_on_github` / `done` / `muted` / `undone` / `unmuted` / `kept_unread` / `unarchived` / `absorbed`. source is `user` (clicked in our app) or `github` (observed/applied remotely — `read_on_github`, plus `unarchived` when a poll resurfaces an archived thread, plus `absorbed` when `_apply_mute_filter` re-suppresses a muted-only re-delivery). Written by `_apply_action`, except `read_on_github` / `unarchived` / `absorbed` which the upsert / `_enrich` / `_apply_mute_filter` path in `app/github.py` writes.
- `user_chat` — free-text per-thread message, source `user`. Written by the composer (see Timeline popover) — both `POST /ai/<id>/chat` (free Note) and `POST /ai/<id>/judge` with text (Send to AI) record one. NULL external_id; each save is its own event (no dedup).

**Timeline popover (`templates/_row.html` + `_timeline_event.html`):** chronological conversation log, rendered in both modes — `_attach_timeline` runs unconditionally. AI verdicts and user chat render as full chat bubbles (AI purple-right, user blue-left); in a verdict the standing `description` is always a recessed aside, and a `reply`, when present, sits above it as the body. Adjacent dismissal user_actions are coalesced — see `_coalesce_user_actions`. **AI mode only:** a composer at the bottom with two acts — Enter saves a free Note (`/ai/<id>/chat`, no AI call), Ctrl+Enter sends to the AI (`/ai/<id>/judge` with the text — "↻ Re-ask" when empty, "Send to AI" with text). Defaulting Enter to the free Note matches the cost asymmetry (a judgment is ~0.5¢ + latency; a note costs nothing and the AI picks it up next time it judges).

**No autonomous re-judgment in v1** — explicit Ask AI / Re-ask only. Per-thread `policy` field intentionally absent. Add when auto re-judging ships; the natural trigger is "N new events since latest ai_verdict" exceeding a threshold.

**Storage:** every API call writes a row to `ai_calls` (full request + response, token breakdown, estimated cost, status). Useful for prompt tuning and the soft daily cap (`AI_DAILY_CAP_USD`, default $2). Past verdicts also live as `ai_verdict` events on `thread_events` — joined via `external_id`.

**Default model:** Haiku 4.5. Configurable via `AI_MODEL`. The system prompt + preferences sit at ~3k tokens after the timeline-interpretation section — still below Haiku's 4096-token cache minimum, so `cache_control` markers don't fire today; they're forward-compatible once preferences grow.

**Mode toggle plumbing:** `triage_mode` is a persisted user setting, not a filter — stored in the `meta` table (`_get_triage_mode` / `_set_triage_mode` in `app/web.py`). The "AI triage mode" row in the toolbar's Options dropdown posts to `/settings/triage_mode`, which flips the value and returns the re-rendered table; the row flips its own checkmark client-side. Per-row HTMX swaps read the mode server-side, so it doesn't need to ride the request.

**History compression** (deferred): timelines are uncompressed in v1. When threads grow long enough to matter, the hook is `kind=ai_recap` events that summarize older history; the AI reads recaps in lieu of the events they replaced.

## How the user works

- One coherent change per commit. Brief message, *why* over *what*. Co-authored by Claude Opus.
- Pushes back on suggestions; expects pushback in return. "Clean wins" is the recurring frame.
- Boring stable tech preferred. No feature-flag / fallback proliferation.
- Often probes edge cases ("what if I do this on mobile?") before settling UX.
