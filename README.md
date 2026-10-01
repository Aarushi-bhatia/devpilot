# DevPilot

An autonomous coding agent for GitHub. Give it a repository and an issue: it plans a fix, waits
for your approval, writes the code, runs the tests in a sandbox, reviews its own diff, and opens
a draft pull request.

```text
Issue → Plan → Approve → Implement → Verify → Review → Draft PR
```

## Example

On [gravitype](https://github.com/kanakOS01/gravitype), issue *"Add a Solarized Dark theme"*:

```text
            planning  Asking nemotron-3-super-120b-a12b (attempt 1/6) ↳ answered in 67s
   awaiting_approval  1. gravitype/tui/styles/themes/solarized_dark.tcss: create the theme
                      2. gravitype/tui/widgets/screens.py: add 'Solarized Dark' to the dropdown
        implementing  Changed: solarized_dark.tcss, screens.py
           verifying  Sandboxed tests passed in python:3.12-slim — 381 passed in 42.02s
           reviewing  Review verdict: approve.
           completed  Draft PR created: https://github.com/Aarushi-bhatia/gravitype/pull/6
```

## Features

- **Human approval** before any file is written — approve, reject, or re-plan with feedback
- **Relevance-ranked context** so the model sees the right files, even in large repositories
- **Anchored edits** — existing files are changed in place, never regenerated
- **Sandboxed tests** in Docker, with no access to the host or your credentials
- **Self-correction** — one debugging pass on failing tests, one revision on review concerns
- **Resilient model calls** — validated retries across several free models, with deadlines
- **Full run history** in SQLite, with a live progress display

## Architecture

```text
cli.py            commands and live progress
orchestrator.py   the state machine
├── openrouter.py   model calls and retries
├── workspace.py    context, edits, verification, commits
│   └── sandbox.py    Docker test runner
├── github.py       issues and pull requests
└── store.py        SQLite run history
```

## Tech stack

Python · Typer · Rich · SQLite · Docker · OpenRouter · GitHub REST API · pytest · GitHub Actions

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
devpilot init
```

Add your keys to `~/.dev-pilot/.env`:

```text
GITHUB_TOKEN=...
OPENROUTER_API_KEY=...
```

## Usage

```bash
devpilot run https://github.com/owner/repo --issue 42
devpilot history
devpilot show <run-id>
devpilot clean
```

Docker is needed for sandboxed tests. Set `DEVPILOT_MODELS` to use specific models instead of
the free defaults.

## License

MIT
