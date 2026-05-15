# Notification triage preferences

Free-form notes for the AI about what counts as signal vs. noise *for you*.
The AI sees this verbatim on every judgment, so be specific. Vague preferences
("I care about quality") are weaker than concrete ones ("Anything touching
`auth/` in `acme/api` is high priority").

Copy this file to `config/preferences.md` and edit it. The app reads
`config/preferences.md`; this `.example.md` ships as a template.

## Areas I care about

What you're working on right now and would want surfaced. Repo paths,
subsystems, project areas, ongoing initiatives. Examples:

- Anything touching `runtime/` or `drivers/` in `acme/engine`
- T-wave annotation, ECG signal processing
- The auth migration work in `myorg/api`

## People to track

Whose threads always matter, regardless of repo. Examples:

- @collaborator-name — co-author on the dissertation
- @manager-name — anything they ping me on is high priority

## Repos that matter

Repos where most threads are worth at least skimming. Examples:

- acme/engine
- myorg/internal-tools

## Noise patterns

What's almost always safe to mark-read or archive. Examples:

- Renovate bot PRs unless they touch a tracked dep
- Stale-bot comments
- CI failures on someone else's PR (when I'm not a reviewer)
- Release notifications from repos I only watch passively

## Other context

Anything else the AI should know. Workflow preferences, time-of-day
considerations, things to ask before muting, etc.
