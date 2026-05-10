# GitHub notification triage

A local Flask app that turns GitHub's notification firehose into a "you should look at this" view. Single user, runs as `source .venv/bin/activate && python -m app run` and serves on `localhost:5734`. Auth via `gh auth token` (with `GITHUB_TOKEN` env override).

The point is *triage* — surface what needs attention, quiet what doesn't — not a fancier inbox. AI integration (next major step) is meant to carry most of the noise reduction; the local infrastructure is the substrate that gives it useful signal to work with.

## Stack

Flask + Jinja + HTMX + Pico CSS + SQLite. Static assets vendored under `static/vendor/`. Python 3.11, stdlib `venv` + `pip`, no lockfile. Schema migrates via a hand-rolled version ladder in `app/db.py` — add a new `SCHEMA_VN` rather than editing existing ones.

## Cross-cutting principles

**GitHub is the source of truth.** The local DB is an augmented cache: stores its own state (favorites, notes, baselines) but doesn't fight GitHub on shared state (read/unread, ignored). The app should remain useful as a "fancier inbox" even with no AI active.

**Indicators persist across user actions.** "+N new comments", `Mentioned`, review-state newness — they stay visible after the user takes an action so the user can see what they just handled. Reset only when actual notification activity shifts them. This shapes the data model: baselines are captured lazily during enrichment, never touched by action handlers. See comments around `_apply_action` and `_enrich`.

**Two-fetch poll.** Each poll runs an unread fetch (default `/notifications` + reconciliation) and a since-fetch (`?all=true&since=<bookmark>`). They catch disjoint cases — see the docstrings in `app/github.py`. Both bounded; deeper history goes through Backfill.

## AI v0 (shipped)

User-triggered, approve-gated per-thread judgment. Lives in the Relevance column, behind a brain-icon mode toggle in the column header. Manual mode shows the rule-based status pill + prose subhead; AI mode replaces both — per-row **Ask AI** button when no verdict, split-pill (popover left + ✓ approve right) when one exists, plus AI-selected signal pills below.

**Inputs:**

- `config/preferences.md` — free-text user prefs (interests, important repos / people, noise patterns). Loaded into the cached system block. `config/preferences.example.md` ships as a template.
- `app/ai_system_prompt.md` — shipped instructions (cost asymmetry, brevity rules, output-field semantics, signal vocabulary). The "do not restate row-visible facts" rule is the load-bearing one — see git history for why.

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

**No autonomous re-judgment in v0** — the original `policy` field is intentionally absent. Add it back when auto re-judging ships.

**Storage:** every API call writes a row to `ai_calls` (full request + response, token breakdown, estimated cost, status). Useful for prompt tuning and the soft daily cap (`AI_DAILY_CAP_USD`, default $2).

**Default model:** Haiku 4.5. Configurable via `AI_MODEL`. The system prompt + preferences sit at ~2.4k tokens — below Haiku's 4096-token cache minimum, so `cache_control` markers don't fire today; they're forward-compatible once preferences grow.

**Approve-button no-op detection:** `_approve_label` in `app/web.py` drops parts of the verdict that are already true on the row (mark_read on a read row, track on a tracked row, …). When everything's a no-op, the button is `disabled` and tells the user to use Dismiss.

**Mode toggle plumbing:** `triage_mode` is part of `_filters_from_request`. The hidden input lives inside `<form id="filters">` (in `index.html`); the brain button in the column header (in `_table.html`) flips it via `toggleTriageMode()` and dispatches a bubbled `change` so HTMX picks it up. Per-row HTMX buttons inherit `hx-include="#filters"` from `<tbody>` so the mode rides every row swap.

## How the user works

- One coherent change per commit. Brief message, *why* over *what*. Co-authored by Claude Opus.
- Pushes back on suggestions; expects pushback in return. "Clean wins" is the recurring frame.
- Boring stable tech preferred. No feature-flag / fallback proliferation.
- Often probes edge cases ("what if I do this on mobile?") before settling UX.
