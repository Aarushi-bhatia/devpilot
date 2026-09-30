# DevPilot

DevPilot is a terminal-first, transparent autonomous GitHub software engineer. It reads a
GitHub issue, plans a change, implements it in an isolated clone, and opens a draft pull
request — pausing for explicit human approval before it writes a single file.

```text
Issue → Understand → Explore → Plan → Approval → Implement → Verify → Review → Draft PR
```

Every state transition is persisted to SQLite as it happens, so any past run can be replayed
with `devpilot show`. The model is pinned to OpenRouter's zero-cost `openrouter/free` router,
so a run can fail for lack of a free model but will never silently fall back to a paid one.

## Requirements

Python 3.11+, git, and — for isolated verification with real test results — Docker. Without
Docker, DevPilot still runs but skips most verification.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
devpilot init
```

`devpilot init` creates `~/.dev-pilot/` (or `DEV_PILOT_HOME`) containing a private `.env`
template and a SQLite run database. Add a GitHub PAT with repository and pull-request access
and an OpenRouter API key to `~/.dev-pilot/.env`. Credentials live outside the repository by
design and the file is created mode `600`.

## Usage

```bash
devpilot run https://github.com/owner/repository --issue 42
devpilot history
devpilot show <run-id>
devpilot clean
```

`run` fetches the issue, clones the repository, and presents a generated plan. Nothing is
written until you approve it. At the prompt, `y` approves, `r` asks for a different plan with
a reason you supply, and anything else ends the run with the clone untouched.

`clean` removes cloned workspaces, which grow once verification installs dependencies. Run
history is kept; the two most recent workspaces are retained unless `--keep` says otherwise.

## Design

**The model's output is data, never instructions.** It returns file contents and nothing else.
Which test command runs is decided by inspecting the clone — `pyproject.toml` means pytest,
`package.json` means npm — never by the model. Every generated path is resolved and rejected
if it escapes the workspace or touches `.git`.

**Approval sits where stopping is still free.** At the gate, only a shallow clone exists. Every
irreversible action — branch, commit, push, pull request — happens after it. The pull request
is opened as a draft, so a second human action is required before anything can merge.

**Untrusted code runs in a container.** Verifying generated code means executing the target
repository's code — its test script, its `conftest.py`, and the `postinstall` hooks of every
package it depends on. DevPilot runs that inside a disposable container with only the clone
mounted, no inherited environment, dropped capabilities, and memory and process limits. The
host filesystem is not mounted, so `~/.dev-pilot/.env` is unreachable even in principle.

Because the blast radius is contained, dependencies can be installed, which is what makes
real test signal possible. Without a container runtime DevPilot falls back to host tooling,
which installs nothing and therefore usually reports verification as skipped.

**The diff is reviewed before the pull request opens.** The model is shown its own diff and
asked whether it addresses the issue and whether it removes anything. The verdict and findings
go into the pull request body ahead of the test output, so a human sees them first. It never
blocks: a change that passed its tests still ships as a draft, with the concerns attached.

**Failures are recorded, not raised.** Any exception is caught and persisted as a `failed`
state with its reason, so a run that dies still leaves a readable history. Model calls carry a
wall-clock deadline as well as a socket timeout, because the free router can trickle bytes
indefinitely without ever tripping a socket read timeout.

## Scope and trade-offs

These are deliberate, not oversights:

- **The review is written by the author.** The model that wrote the change also reviews it, so
  it shares the blind spots that produced the change. It is a prompt to look, not assurance,
  and it never blocks the pull request.
- **Without Docker there is no isolation.** The fallback path runs host tooling as the current
  user with the full environment, and installs nothing, so verification is usually skipped.
  Install Docker to get both isolation and real test results.
- **The sandbox has network access**, which dependency installation requires. A hostile
  package cannot reach the host, but it can reach the internet.
- **Context is size-limited.** The coder receives up to 40 KB of existing source, ordered by
  relevance to the issue. A repository larger than that will have its least relevant files
  dropped, so an issue that describes the change vaguely gets a worse selection.

## Roadmap

1. Review by a second, different model, so the reviewer does not share the author's blind spots.
2. An offline sandbox mode that pre-fetches dependencies, so the suite runs with no network.
3. A web view over the persisted run timeline.

## Development

```bash
pip install -e ".[dev]"
pytest
```

## License

MIT — see [LICENSE](LICENSE).
