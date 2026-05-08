# gh-notification-auto-triage

Local GitHub notification triage app. Runs a Flask server on `localhost:5734` that
polls your GitHub notifications every 5 minutes and shows them in a table.

## Setup

Requires Python 3.11+ and the [GitHub CLI](https://cli.github.com/) authenticated
with the `notifications` scope:

```bash
gh auth login
gh auth refresh -s notifications
```

Then:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
python -m app run
```

Open http://127.0.0.1:5734.

## Configuration

Optional `.env` (see `.env.example`):

- `GITHUB_TOKEN` — overrides `gh auth token` if set
- `PORT` — server port (default 5734)
