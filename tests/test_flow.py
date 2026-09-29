"""State-machine, persistence and GitHub-client tests.

These exercise the orchestrator against fakes rather than the network, so the whole flow —
including approval, re-planning and failure handling — is covered without a model call, a
container, or a GitHub token.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from devpilot import orchestrator as orchestrator_module
from devpilot.github import GitHubClient, GitHubError, Issue
from devpilot.models import Run, RunState
from devpilot.orchestrator import LiveOrchestrator
from devpilot.store import RunStore

ISSUE = Issue(title="Add a LICENSE file", body="Add MIT.", url="https://example.invalid/1")


class FakeGitHub:
    def __init__(self) -> None:
        self.created: list[tuple[str, str]] = []

    def get_issue(self, owner: str, repository: str, number: int) -> Issue:
        return ISSUE

    def create_draft_pr(self, owner: str, repository: str, title: str, body: str, head: str, base: str) -> str:
        self.created.append((title, body))
        return "https://github.com/o/r/pull/1"


def build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **behaviour) -> tuple[LiveOrchestrator, FakeGitHub, list]:
    """Assemble an orchestrator whose every external effect is replaced by a fake."""
    events: list = []
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()

    def fake_clone(url, destination, token):
        destination.mkdir(parents=True)
        (destination / "README.md").write_text("seed\n")

    for name, replacement in {
        "clone": fake_clone,
        "repository_snapshot": lambda workspace: "Files:\nREADME.md",
        "source_context": lambda workspace, text: "--- README.md ---\nseed",
        "create_branch": lambda workspace, branch: None,
        "apply_changes": lambda workspace, changes: [change["path"] for change in changes],
        "verify": behaviour.get("verify", lambda workspace: "Sandboxed tests passed"),
        "changed_files": lambda workspace, paths=None: "M LICENSE",
        "diff": lambda workspace, paths: "+MIT",
        "default_branch": lambda workspace: "main",
        "commit_and_push": lambda workspace, branch, message, token, paths: None,
    }.items():
        monkeypatch.setattr(orchestrator_module, name, replacement)

    agent = LiveOrchestrator.__new__(LiveOrchestrator)
    agent.store = RunStore(tmp_path / "runs.sqlite3")
    agent.on_event = events.append
    agent.github = FakeGitHub()
    agent.github_token, agent.openrouter_key, agent.workspaces = "t", "k", workspace_root
    monkeypatch.setattr(agent, "_plan", lambda issue, snapshot, rejected=None: [f"step {len(rejected or [])}", "step b"])
    monkeypatch.setattr(agent, "_changes", lambda *a, **k: [{"path": "LICENSE", "content": "MIT"}])
    monkeypatch.setattr(agent, "_review", behaviour.get(
        "review", lambda issue, patch, verification: {"verdict": "approve", "summary": "Fine.", "findings": []}))
    return agent, agent.github, events


def states(run: Run) -> list[str]:
    return [event.state.value for event in run.events]


def test_approved_run_reaches_a_draft_pull_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent, github, _ = build(tmp_path, monkeypatch)
    run = agent.run("https://github.com/o/r", 1, lambda _: True)
    assert run.state is RunState.COMPLETED
    assert states(run)[:5] == ["understanding", "exploring", "planning", "awaiting_approval", "implementing"]
    assert len(github.created) == 1


def test_rejection_writes_nothing_and_never_reaches_the_pull_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate exists so that declining costs nothing; that must hold in the state machine."""
    agent, github, _ = build(tmp_path, monkeypatch)
    run = agent.run("https://github.com/o/r", 1, lambda _: False)
    assert run.state is RunState.REJECTED
    assert "implementing" not in states(run)
    assert github.created == []


def test_a_reason_triggers_replanning_and_then_proceeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent, github, _ = build(tmp_path, monkeypatch)
    answers = iter(["use a different approach", True])
    run = agent.run("https://github.com/o/r", 1, lambda _: next(answers))
    assert run.state is RunState.COMPLETED
    assert states(run).count("awaiting_approval") == 2
    assert run.plan == ["step 1", "step b"]  # the second plan saw one rejection


def test_replanning_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An always-rejecting handler must not loop forever against a paid-per-call model."""
    monkeypatch.setattr(orchestrator_module, "MAX_REPLANS", 2)
    agent, github, _ = build(tmp_path, monkeypatch)
    run = agent.run("https://github.com/o/r", 1, lambda _: "never happy")
    assert run.state is RunState.REJECTED
    assert states(run).count("awaiting_approval") == 3
    assert github.created == []


def test_an_unexpected_failure_is_recorded_rather_than_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, github, _ = build(tmp_path, monkeypatch)
    monkeypatch.setattr(orchestrator_module, "clone", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    run = agent.run("https://github.com/o/r", 1, lambda _: True)
    assert run.state is RunState.FAILED
    assert "disk full" in run.events[-1].message


def test_a_failed_review_does_not_discard_a_verified_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Losing the reviewer must not throw away work whose tests already passed."""
    def exploding_review(issue, patch, verification):
        raise RuntimeError("router unavailable")

    agent, github, _ = build(tmp_path, monkeypatch, review=exploding_review)
    run = agent.run("https://github.com/o/r", 1, lambda _: True)
    assert run.state is RunState.COMPLETED
    assert run.review["verdict"] == "unclear"
    assert "router unavailable" in github.created[0][1]


def test_the_run_survives_a_save_and_reload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent, _, _ = build(tmp_path, monkeypatch)
    run = agent.run("https://github.com/o/r", 1, lambda _: True)
    reloaded = agent.store.get(run.id)
    assert reloaded is not None
    assert reloaded.state is run.state
    assert reloaded.plan == run.plan
    assert reloaded.review == run.review
    assert len(reloaded.events) == len(run.events)


def test_the_store_orders_runs_by_recency(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.sqlite3")
    for index in range(3):
        run = Run(repository_url="https://github.com/o/r", issue_number=index)
        run.created_at = f"2026-01-0{index + 1}T00:00:00+00:00"
        store.save(run)
    assert [item.issue_number for item in store.recent(2)] == [2, 1]


def test_a_pull_request_number_is_not_accepted_as_an_issue(monkeypatch: pytest.MonkeyPatch) -> None:
    """GitHub numbers issues and pull requests in one sequence, so --issue 2 may be a PR."""
    client = GitHubClient("token")
    monkeypatch.setattr(client, "_request", lambda *a, **k: {"title": "t", "pull_request": {}, "html_url": "u"})
    with pytest.raises(GitHubError, match="pull request"):
        client.get_issue("o", "r", 2)


def test_a_client_without_a_token_fails_immediately() -> None:
    with pytest.raises(GitHubError, match="GITHUB_TOKEN"):
        GitHubClient("")


def test_repository_urls_are_validated_before_any_work_happens() -> None:
    assert LiveOrchestrator._coordinates("https://github.com/owner/repo") == ("owner", "repo")
    with pytest.raises(ValueError):
        LiveOrchestrator._coordinates("https://gitlab.com/owner/repo")


def test_a_draft_pull_request_adopts_one_that_already_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    """A retried POST can land after the original succeeded; the run must not fail for it."""
    client = GitHubClient("token")

    def responses(method, path, payload=None):
        if method == "POST":
            raise GitHubError("GitHub API 422: A pull request already exists for o:branch.")
        return [{"html_url": "https://github.com/o/r/pull/9"}]

    monkeypatch.setattr(client, "_request", responses)
    assert client.create_draft_pr("o", "r", "t", "b", "branch", "main") == "https://github.com/o/r/pull/9"


def test_the_event_log_records_every_transition_in_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent, _, events = build(tmp_path, monkeypatch)
    run = agent.run("https://github.com/o/r", 1, lambda _: True)
    assert [event.state for event in events] == [event.state for event in run.events]
    assert json.loads(json.dumps(states(run)))[-1] == "completed"


def test_a_trickling_response_is_abandoned_at_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """A socket timeout only fires on a total stall, so a slow drip must be cut off separately."""
    import time as clock
    import urllib.request as request_module

    from devpilot.openrouter import OpenRouterError, post

    monkeypatch.setattr(request_module, "urlopen", lambda *a, **k: clock.sleep(30))
    started = clock.monotonic()
    with pytest.raises(OpenRouterError, match="did not respond within"):
        post(request_module.Request("https://example.invalid"), deadline=1)
    assert clock.monotonic() - started < 5, "the caller must not wait for the abandoned request"
