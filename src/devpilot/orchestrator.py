from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from .github import GitHubClient, Issue
from .models import Event, Run, RunState
from .openrouter import json_call
from .store import RunStore
from .workspace import (
    apply_changes, changed_files, clone, commit_and_push, create_branch, default_branch,
    repository_snapshot, source_context, verify,
)

EventHandler = Callable[[Event], None]
ApprovalHandler = Callable[[Run], bool]

SYSTEM = """You are DevPilot, a careful software engineer. Return only valid JSON matching the requested shape.
Do not use Markdown. Make focused, minimal changes. Never include secrets. """


class LiveOrchestrator:
    def __init__(self, store: RunStore, on_event: EventHandler, github_token: str, openrouter_key: str, workspaces: Path) -> None:
        self.store, self.on_event = store, on_event
        self.github = GitHubClient(github_token)
        self.github_token, self.openrouter_key, self.workspaces = github_token, openrouter_key, workspaces

    def _transition(self, run: Run, state: RunState, message: str) -> None:
        event = run.transition(state, message)
        self.store.save(run)
        self.on_event(event)

    @staticmethod
    def _coordinates(url: str) -> tuple[str, str]:
        match = re.fullmatch(r"https://github\.com/([\w.-]+)/([\w.-]+)", url)
        if not match:
            raise ValueError("Expected https://github.com/owner/repository")
        return match.group(1), match.group(2)

    def _plan(self, issue: Issue, snapshot: str) -> list[str]:
        response = json_call(self.openrouter_key, SYSTEM, f"""Create an implementation plan for this GitHub issue.
Issue title: {issue.title}
Issue body: {issue.body}
Repository information:
{snapshot[:24000]}
Return exactly {{"plan":["step", "step"]}} with 2–5 concrete steps.""")
        plan = response.get("plan")
        if not isinstance(plan, list) or not 2 <= len(plan) <= 5 or not all(isinstance(item, str) for item in plan):
            raise ValueError("Planner returned an invalid plan.")
        return plan

    def _changes(self, issue: Issue, plan: list[str], snapshot: str, sources: str, feedback: str = "") -> list[dict]:
        response = json_call(self.openrouter_key, SYSTEM, f"""Implement this issue in the cloned repository.
Issue: {issue.title}\n{issue.body}
Approved plan: {plan}
Repository information: {snapshot[:8000]}

Current contents of existing files. Each entry you return REPLACES the whole file, so any file
you touch must be returned complete, preserving every existing line you are not changing:
{sources[:40000]}
{('Previous verification failed:\n' + feedback[-5000:]) if feedback else ''}
Return exactly {{"changes":[{{"path":"relative/path", "content":"complete new file content"}}],"summary":"short"}}.
Return 1–8 complete file replacements or additions. Do not include a test command.""")
        changes = response.get("changes")
        if not isinstance(changes, list):
            raise ValueError("Coder returned no changes.")
        return changes

    def run(self, repository_url: str, issue_number: int, approve: ApprovalHandler) -> Run:
        """Execute one run, recording any failure as a persisted state rather than a traceback."""
        run = Run(repository_url=repository_url, issue_number=issue_number)
        self.store.save(run)
        try:
            return self._execute(run, repository_url, issue_number, approve)
        except Exception as error:
            self._transition(run, RunState.FAILED, f"{type(error).__name__}: {error}")
            return run

    def _execute(self, run: Run, repository_url: str, issue_number: int, approve: ApprovalHandler) -> Run:
        owner, repository = self._coordinates(repository_url)
        self._transition(run, RunState.UNDERSTANDING, f"Fetching GitHub issue #{issue_number}.")
        issue = self.github.get_issue(owner, repository, issue_number)
        workspace = self.workspaces / run.id
        self._transition(run, RunState.EXPLORING, "Cloning repository into an isolated DevPilot workspace.")
        clone(repository_url, workspace, self.github_token)
        snapshot = repository_snapshot(workspace)
        sources = source_context(workspace, f"{issue.title}\n{issue.body}")
        self._transition(run, RunState.PLANNING, "Requesting a plan from OpenRouter's free-model router.")
        run.plan = self._plan(issue, snapshot)
        self.store.save(run)
        self._transition(run, RunState.AWAITING_APPROVAL, "Plan ready. No files have been changed.")
        if not approve(run):
            self._transition(run, RunState.REJECTED, "Plan rejected by user. The isolated clone was left untouched.")
            return run
        branch = f"devpilot/issue-{issue_number}-{run.id}"
        create_branch(workspace, branch)
        self._transition(run, RunState.IMPLEMENTING, "Generating and applying confined file changes.")
        changes = self._changes(issue, run.plan, snapshot, sources)
        # Accumulated across the debug pass: verification installs dependencies into the clone,
        # so only paths DevPilot authored may be staged, and a debug pass must not drop the first.
        authored = dict.fromkeys(apply_changes(workspace, changes))
        self._transition(run, RunState.IMPLEMENTING, f"Changed: {', '.join(authored)}")
        verification = ""
        for attempt in range(2):
            self._transition(run, RunState.VERIFYING, "Running locally discovered verification commands.")
            try:
                verification = verify(workspace)
                break
            except Exception as error:
                verification = str(error)
                if attempt:
                    self._transition(run, RunState.FAILED, f"Verification failed after one debug attempt: {verification[-500:]}")
                    return run
                self._transition(run, RunState.IMPLEMENTING, "Verification failed; requesting one constrained debugging pass.")
                retried = apply_changes(workspace, self._changes(issue, run.plan, snapshot, sources, verification))
                authored.update(dict.fromkeys(retried))
                self._transition(run, RunState.IMPLEMENTING, f"Debug pass changed: {', '.join(retried)}")
        paths = list(authored)
        self._transition(run, RunState.REVIEWING, f"Reviewing diff and verification result. {changed_files(workspace, paths)}")
        base = default_branch(workspace)
        self._transition(run, RunState.CREATING_DRAFT_PR, "Committing, pushing, and creating a GitHub draft PR.")
        commit_and_push(workspace, branch, f"feat: address issue #{issue_number}", self.github_token, paths)
        pr_url = self.github.create_draft_pr(owner, repository, f"Draft: {issue.title}", f"Closes #{issue_number}\n\nDevPilot verification:\n{verification[-2000:]}", branch, base)
        self._transition(run, RunState.COMPLETED, f"Draft PR created: {pr_url}")
        return run
