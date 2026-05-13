# Eddy for GitHub

Local GitHub notification triage app — the calm pocket beside the firehose. Runs a
Flask server on `localhost:5734` that polls your GitHub notifications every 5 minutes
and shows them in a table.

## Setup

Requires Python 3.11+.

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
python -m app run
```

On first launch Eddy opens github.com in your browser and asks you to
authorize the app (scopes: `notifications`, `read:org`). The token is
stored in `data/auth.json` (mode 0600) and reused on subsequent launches.
Revoke any time at https://github.com/settings/applications.

Open http://127.0.0.1:5734.

Headless / CI: set `GITHUB_TOKEN` in `.env` to skip the browser prompt.

## Configuration

Optional `.env` (see `.env.example`):

- `GITHUB_TOKEN` — skip the device-flow prompt and use this token instead
- `EDDY_OAUTH_CLIENT_ID` — override the OAuth Client ID (for forks)
- `PORT` — server port (default 5734)
- `ANTHROPIC_API_KEY` — required for the **Ask AI** triage feature
- `AI_MODEL` — overrides the default model (`claude-haiku-4-5`)
- `AI_DAILY_CAP_USD` — soft daily spend cap for AI calls (default `2.0`).
  When reached, **Ask AI** returns an error toast until the next day or you
  raise the cap.

## AI triage

The **Relevance** column has a brain-icon toggle in its header. Click it to
swap the column into AI mode: rule-based status pills are replaced with
per-row **Ask AI** buttons. Click one and Claude returns a verdict —
priority (0.0–1.0 score), proposed action (`mark_read` / `mute` / `archive`
/ `none`), tracked-flag change, up to 3 relevant-signal pills, and a short
description. Nothing mutates until you approve.

The verdict shows as a split pill: the left half opens a detail popover
(description, model, age, **Re-ask** / **Dismiss**); the right ✓ half
applies the proposed actions directly. The ✓ tooltip says exactly what it
will do (e.g. *"Mark read and track"*) and disables itself when every
proposed change is already in effect on the row.

Setup:

1. Set `ANTHROPIC_API_KEY` in `.env`.
2. `cp config/preferences.example.md config/preferences.md` and edit
   `config/preferences.md` with what you care about (interests, important
   repos and people, noise patterns). The AI reads this on every judgment.
3. Toggle the brain icon in the Relevance column header, then **Ask AI**
   on any row.

Every API call is logged in `data/notifications.db` (`ai_calls` table) with
the full request, response, token breakdown, and estimated cost — useful
for tuning the prompt and verifying the daily cap.
