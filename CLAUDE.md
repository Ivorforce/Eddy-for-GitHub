# GitHub notification triage

A local Flask app at `localhost:5734` that turns GitHub's notification firehose into a "you should look at this" view. Single user, foreground `python -m app run`. Auth via `gh auth token` (with `GITHUB_TOKEN` env override). Stack: Flask + Jinja + HTMX + Pico CSS + SQLite, vendored where applicable, no lockfile.

Schema migrates via a hand-rolled version ladder in `app/db.py`. Add a new `SCHEMA_VN` and `init()` step rather than editing previous ones.

## Framing — don't relitigate

**"Should you look at this?", not "manage your inbox."** Low-signal notifications should stay quiet; high-signal ones should pop. GitHub is the source of truth; the local DB is an augmented cache that adds derived state, persistence, and AI-friendly hooks.

**Read vs Muted.** Mutually exclusive radio states (plus implicit "unread"). *Read* = "skip this update, still want the thread." *Muted* = "stop notifying me about this thread." Done was dropped — Read keeps a row visible-faded which covers history-scrolling. Click title = visit + auto-mark-read.

**Indicators persist across Read.** `+N new comments`, the `pill-new` dot on review state, the `Mentioned` flag — they stay visible *after* the user takes an action so they can see what they just handled. Reset only on genuine new activity. Baselines (`baseline_comments`, `baseline_review_state`) captured lazily in `_enrich` via `COALESCE`; actions don't touch them.

**Two-fetch poll.** `_fetch_unread` (default endpoint + reconcile-missing-as-read) is the only path that catches read-state changes on threads with no new activity ("clicked through on mobile without commenting"). `_fetch_since` (`?all=true&since=<bookmark>`) catches arrivals we never saw because they were created and read on another client. Both bounded ~100 items per call. Bigger backfills route through Backfill.

**Reactions: max-per-bucket aggregation.** Same user can react with multiple positives — sum overcounts. We approximate distinct positive reacters with `max(positive_emojis)`, same for negative. Eyes are independent.

**Reception vs Interest** are two distinct axes:
- *Reception* (▲/▼) = sentiment polarity, saturated colors, scan-worthy.
- *Interest* = comments (+ new badge) + max-aggregated reactions + `unique_commenters`. Distinguishes "5 people arguing" from "20 people engaged."

**Saturation as visual hierarchy.** LOC diff is muted (factual metric). Reaction triangles are saturated bold (sentiment). Same green/red palette, different roles → different weights.

**Time-bucket grouping** (Today / Yesterday / This week / This month / Earlier) replaces an Updated column. Sort = `updated_at DESC` within each bucket. Favorites and yours-rows tint via left-border (gold / blue) but don't pin.

**Status column priority** (top→bottom): mergeable warning (`conflicts` / `CI failing` / `behind` — `blocked` was dropped, duplicates "review needed") → review state (Approved / Changes requested) → action-needed (assigned / review_you / review_team) → mention.

**Author indicators**, icon-only, left of the login: shield-check (member), sparkle (first-timer), filled-person (you — trumps the others).

**Cross-thread state lives in the `people` table** (favorites + notes). Per-thread author metadata stays in `details_json`. Toggling a person favorite fires `HX-Trigger: personFavoriteChanged` so every row showing that person updates immediately, not just the originating row.

**Status dot** (top right): green idle, orange pulsing (300ms minimum visible), red error/offline. Sticky while `navigator.onLine` is false. Replaces the old inline error dump.

**Note autosave** uses HTMX `keyup`-debounced + `blur`, plus `navigator.sendBeacon()` on `pagehide` so dirty drawer edits survive reload/tab-close.

## Planned: AI v0

Foundation pieces, write *before* implementing the prompt:
- `config/preferences.md` — free text: what the user cares about, signals to weight, notable people, repos that matter.
- `config/subsystems.md` — Godot subsystem map (paths → topic labels). Anchors the AI to project-specific structure.

Per-thread judgment, structured tool-use output, cached on the row:
```
{ action_now, user_priority, summary, policy, rationale }
```
Policies drive auto-action on subsequent events ("on_new_comment: auto_mark_read") to keep API cost bounded — re-judge only when the policy says "reconsider."

Schema columns already reserved: `notifications.note_ai`, `people.note_ai`. Add an `ai_calls` table when wiring up — log every prompt + response for prompt tuning later.

Default model: Haiku. Configurable per call. Soft daily cost cap with clear logging when hit.

## How the user works

- One coherent change per commit. Brief message, "why" over "what". Co-authored by Claude Opus.
- Pushes back on suggestions; expects me to push back too. "Clean wins" is a recurring phrase.
- Boring stable tech preferred. Avoids feature-flag / fallback proliferation.
- Often probes edge cases ("what if I do this on mobile?") before settling UX.
- macOS, terminal-fluent.

## Pointers (don't memorize, look up)

- Schema + migrations: `app/db.py`.
- Row composition + filters + routes: `app/web.py`.
- API plumbing: `app/github.py` — `_fetch_unread`, `_fetch_since`, `backfetch`, `_enrich`.
- Main row layout: `templates/_row.html`. Bucket separators: `_table.html`. Toolbar + filters: `index.html`. Global CSS + JS handlers: `base.html`.
