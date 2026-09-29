from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from .github import GitHubClient, Issue
from .models import Event, Run, RunState
from .openrouter import json_call
from .store import RunStore
from .workspace import (
    apply_changes, changed_files, clone, commit_and_push, create_branch, default_branch, diff,
    repository_snapshot, source_context, verify,
)

EventHandler = Callable[[Event], None]
# True approves; a string rejects with a reason and asks for another plan; False ends the run.
ApprovalHandler = Callable[[Run], "bool | str"]
MAX_REPLANS = 3

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

    def _plan(self, issue: Issue, snapshot: str, rejected: list[tuple[list[str], str]] | None = None) -> list[str]:
        history = "".join(
            f"\nYou previously proposed: {plan}\nThe user rejected it because: {reason or 'no reason given'}"
            for plan, reason in (rejected or [])
        )
        response = json_call(self.openrouter_key, SYSTEM, f"""Create an implementation plan for this GitHub issue.
Issue title: {issue.title}
Issue body: {issue.body}
Repository information:
{snapshot[:24000]}{history}
{'Propose a genuinely different approach that addresses the rejection.' if history else ''}
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

    def _review(self, issue: Issue, patch: str, verification: str) -> dict:
        """Ask the model to review its own diff before the pull request is opened.

        A self-review catches the failure the test suite cannot: a change that passes because
        it does something other than what the issue asked for. The verdict never blocks the
        pull request — it is written into the body so the human reviewer sees it first.
        """
        response = json_call(self.openrouter_key, SYSTEM, f"""Review this diff as a critical reviewer.
Issue: {issue.title}\n{issue.body}
Verification result: {verification[-2000:]}
Diff:
{patch}
Judge only what the diff shows. Does it address the issue, and does it break or delete anything?
Return exactly {{"verdict":"approve"|"concerns","summary":"one sentence","findings":["finding"]}}.
Use "concerns" if anything is removed, incomplete, or unrelated to the issue. Return at most 5 findings.""")
        verdict = response.get("verdict")
        findings = response.get("findings")
        return {
            "verdict": verdict if verdict in {"approve", "concerns"} else "unclear",
            "summary": str(response.get("summary", "")).strip(),
            "findings": [str(item) for item in findings][:5] if isinstance(findings, list) else [],
        }

    @staticmethod
    def _review_body(review: dict) -> str:
        heading = {"approve": "No concerns raised", "concerns": "Concerns raised"}.get(review["verdict"], "Inconclusive")
        lines = [f"**Automated review — {heading}.** {review['summary']}".rstrip()]
        lines += [f"- {finding}" for finding in review["findings"]]
        lines.append("\nThis review was written by the same model that wrote the change; treat it as a prompt to look, not as assurance.")
        return "\n".join(lines)

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
        rejected: list[tuple[list[str], str]] = []
        while True:
            self._transition(run, RunState.PLANNING, "Requesting a plan from OpenRouter's free-model router.")
            run.plan = self._plan(issue, snapshot, rejected)
            self.store.save(run)
            self._transition(run, RunState.AWAITING_APPROVAL, "Plan ready. No files have been changed.")
            decision = approve(run)
            if decision is True:
                break
            # A handler may return a rejection reason instead of False, asking for another plan.
            # Nothing has been written yet, so re-planning costs one model call and no cleanup.
            if not isinstance(decision, str) or len(rejected) >= MAX_REPLANS:
                self._transition(run, RunState.REJECTED, "Plan rejected by user. The isolated clone was left untouched.")
                return run
            rejected.append((run.plan, decision))
            self._transition(run, RunState.PLANNING, f"Re-planning ({len(rejected)}/{MAX_REPLANS}): {decision}")
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
        self._transition(run, RunState.REVIEWING, f"Reviewing the diff. {changed_files(workspace, paths)}")
        try:
            review = self._review(issue, diff(workspace, paths), verification)
        except Exception as error:
            # A review that cannot be produced must not discard a change that already passed
            # its tests; the pull request opens with the failure recorded in its place.
            review = {"verdict": "unclear", "summary": f"The review could not be produced: {error}", "findings": []}
        run.review = review
        self.store.save(run)
        self._transition(run, RunState.REVIEWING, f"Review verdict: {review['verdict']}. {review['summary']}")
        for finding in review["findings"]:
            self._transition(run, RunState.REVIEWING, f"  · {finding}")
        base = default_branch(workspace)
        self._transition(run, RunState.CREATING_DRAFT_PR, "Committing, pushing, and creating a GitHub draft PR.")
        commit_and_push(workspace, branch, f"feat: address issue #{issue_number}", self.github_token, paths)
        body = f"Closes #{issue_number}\n\n{self._review_body(review)}\n\nDevPilot verification:\n{verification[-2000:]}"
        pr_url = self.github.create_draft_pr(owner, repository, f"Draft: {issue.title}", body, branch, base)
        self._transition(run, RunState.COMPLETED, f"Draft PR created: {pr_url}")
        return run
