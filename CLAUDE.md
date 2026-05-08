# GitHub notification triage

A local Flask app that turns GitHub's notification firehose into a "you should look at this" view. Single user, runs as `python -m app run` and serves on `localhost:5734`. Auth via `gh auth token` (with `GITHUB_TOKEN` env override).

The point is *triage* — surface what needs attention, quiet what doesn't — not a fancier inbox. AI integration (next major step) is meant to carry most of the noise reduction; the local infrastructure is the substrate that gives it useful signal to work with.

## Stack

Flask + Jinja + HTMX + Pico CSS + SQLite. Static assets vendored under `static/vendor/`. Python 3.11, stdlib `venv` + `pip`, no lockfile. Schema migrates via a hand-rolled version ladder in `app/db.py` — add a new `SCHEMA_VN` rather than editing existing ones.

## Cross-cutting principles

**GitHub is the source of truth.** The local DB is an augmented cache: stores its own state (favorites, notes, baselines) but doesn't fight GitHub on shared state (read/unread, ignored). The app should remain useful as a "fancier inbox" even with no AI active.

**Indicators persist across user actions.** "+N new comments", `Mentioned`, review-state newness — they stay visible after the user takes an action so the user can see what they just handled. Reset only when actual notification activity shifts them. This shapes the data model: baselines are captured lazily during enrichment, never touched by action handlers. See comments around `_apply_action` and `_enrich`.

**Two-fetch poll.** Each poll runs an unread fetch (default `/notifications` + reconciliation) and a since-fetch (`?all=true&since=<bookmark>`). They catch disjoint cases — see the docstrings in `app/github.py`. Both bounded; deeper history goes through Backfill.

## Planned: AI v0

Foundation pieces, write *before* implementing the prompt:

- `config/preferences.md` — free text: what the user cares about, signals to weight, notable people, repos that matter.
- `config/subsystems.md` — for Godot, the subsystem map (paths → topic labels). Anchors the AI to project-specific structure.

Per-thread judgment, structured tool-use output, cached on the row:

```
{ action_now, user_priority, summary, policy, rationale }
```

The per-thread `policy` drives auto-action on subsequent events (`on_new_comment: auto_mark_read`) to keep API cost bounded — re-judge only when policy says `reconsider`.

Schema slots already reserved: `notifications.note_ai`, `people.note_ai`. Add an `ai_calls` table for prompt+response logging when wiring up — that's what we'll need to tune the prompt later.

Default model: Haiku. Configurable per call. Soft daily cost cap with clear logging when hit.

## How the user works

- One coherent change per commit. Brief message, *why* over *what*. Co-authored by Claude Opus.
- Pushes back on suggestions; expects pushback in return. "Clean wins" is the recurring frame.
- Boring stable tech preferred. No feature-flag / fallback proliferation.
- Often probes edge cases ("what if I do this on mobile?") before settling UX.
