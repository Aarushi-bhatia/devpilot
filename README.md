# DevPilot

DevPilot is a terminal-first, transparent autonomous GitHub software engineer.
It is designed around an explicit agent state graph:

```text
Issue → Understand → Explore → Plan → Approval → Implement → Verify → Review → Draft PR
```

DevPilot is live and deliberately **free-only**: it uses GitHub's API with your PAT,
an isolated clone under `~/.dev-pilot/workspaces/`, and OpenRouter's `openrouter/free`
router. It makes real changes, runs safe discovered checks, pushes a branch, and opens
a real draft PR after your approval. Every run is persisted for later inspection.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
devpilot init
```

`devpilot init` creates `~/.dev-pilot/` (or the value of `DEV_PILOT_HOME`) with a
configuration file, a private `.env` template, and a SQLite run database. Add a GitHub
PAT with repository/PR access and an OpenRouter API key to `~/.dev-pilot/.env`; never
store them in a repository. The model is hard-coded to `openrouter/free`, so DevPilot
will not select a paid model.

## Demo workflow

```bash
devpilot run https://github.com/owner/repository --issue 42
devpilot history
devpilot show <run-id>
```

The `run` command fetches the issue and clones the repository before presenting a
generated plan. It waits for interactive approval before creating a branch or changing
any files. A free model can be rate-limited or unavailable; that will stop the run
without falling back to a paid model.

## Roadmap

1. Add a FastAPI dashboard over the persisted run timeline.
2. Add stronger sandboxing for test execution.
3. Add language-specific test configuration and test selection.
