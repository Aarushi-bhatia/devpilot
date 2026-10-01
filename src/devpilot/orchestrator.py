from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from .github import GitHubClient, Issue
from .models import Event, Run, RunState
from .openrouter import OpenRouterError, Tick, json_call
from .store import RunStore
from .workspace import (
    apply_changes, changed_files, clone, commit_and_push, create_branch, default_branch, diff, problem,
    repository_snapshot, source_context, verify,
)

EventHandler = Callable[[Event], None]
# True approves; a string rejects with a reason and asks for another plan; False ends the run.
ApprovalHandler = Callable[[Run], "bool | str"]
MAX_REPLANS = 3

SYSTEM = """You are DevPilot, a careful software engineer. Return only valid JSON matching the requested shape.
Do not use Markdown. Make focused, minimal changes. Never include secrets. """


def valid_plan(response: dict) -> bool:
    plan = response.get("plan")
    return isinstance(plan, list) and 1 <= len(plan) <= 5 and all(isinstance(step, str) for step in plan)


# Steps that begin with one of these describe looking at something rather than changing it.
NON_CHANGING = frozenset(
    "examine inspect read review locate find understand check verify confirm run test start open commit push ensure".split()
)
PATH_TOKEN = re.compile(r"[\w.-]+(?:/[\w.-]+)+\.\w+")


def uncovered(plan: list[str], paths: set[str]) -> str | None:
    """Return why a change leaves part of the approved plan undone, or None if it covers it.

    A step that names files is satisfied when at least one of them is changed, so "create
    themes/solarized.tcss like themes/nord.tcss" is met by the new file alone, while "register
    it in widgets/screens.py" can only be met by editing screens.py. Without this check the
    retry loop quietly favours the reply that skips the hard file: an edit whose anchor fails
    is rejected, and a reply that never attempted the edit passes.
    """
    for number, step in enumerate(plan, 1):
        words = re.sub(r"^\s*step\s*\d+\s*[:.)-]\s*", "", step, flags=re.IGNORECASE).split()
        if words and words[0].lower().strip("*`'\"") in NON_CHANGING:
            continue
        named = {token.removeprefix("./") for token in PATH_TOKEN.findall(step)}
        if named and not named & paths:
            return (f"step {number} of the approved plan changes {', '.join(sorted(named))}, "
                    "but the reply leaves it untouched; implement every step")
    return None


def valid_changes(response: dict) -> bool:
    """Accept a non-empty list of changes, each either a whole file or an anchored edit, so a
    reply shaped differently is retried against another model rather than ending the run."""
    changes = response.get("changes")
    if not isinstance(changes, list) or not changes:
        return False
    return all(
        isinstance(item, dict) and isinstance(item.get("path"), str) and (
            isinstance(item.get("content"), str)
            or (isinstance(item.get("find"), str) and isinstance(item.get("replace"), str))
        )
        for item in changes
    )


def outcome(verification: str) -> str:
    """Summarise a verification result in one line: its headline plus any test tally."""
    lines = [line.strip() for line in verification.splitlines() if line.strip()]
    if not lines:
        return "Verification complete."
    headline = lines[0].rstrip(":")
    tally = next((line for line in reversed(lines) if re.search(r"\b\d+ (passed|failed)\b", line)), "")
    return f"{headline} — {tally}" if tally and tally != lines[0] else headline


class LiveOrchestrator:
    # Class-level defaults so an instance assembled without __init__ (as the tests do) still has them.
    on_wait: Tick | None = None
    _active: Run | None = None

    def __init__(
        self, store: RunStore, on_event: EventHandler, github_token: str, openrouter_key: str, workspaces: Path,
        on_wait: Tick | None = None,
    ) -> None:
        self.store, self.on_event, self.on_wait = store, on_event, on_wait
        self.github = GitHubClient(github_token)
        self.github_token, self.openrouter_key, self.workspaces = github_token, openrouter_key, workspaces

    def _transition(self, run: Run, state: RunState, message: str, detail: bool = False) -> None:
        event = run.transition(state, message, detail)
        self.store.save(run)
        self.on_event(event)

    def _progress(self, message: str) -> None:
        """Record a model-call progress line under whichever state the run is currently in."""
        if self._active is not None:
            self._transition(self._active, self._active.state, message, detail=True)

    def _ask(self, prompt: str, shape: Callable[[dict], bool] | None = None) -> dict:
        return json_call(self.openrouter_key, SYSTEM, prompt, shape=shape, progress=self._progress, tick=self.on_wait)

    @staticmethod
    def _coordinates(url: str) -> tuple[str, str]:
        match = re.fullmatch(r"https://github\.com/([\w.-]+)/([\w.-]+)", url)
        if not match:
            raise ValueError("Expected https://github.com/owner/repository")
        return match.group(1), match.group(2)

    def _plan(self, issue: Issue, snapshot: str, sources: str,
              rejected: list[tuple[list[str], str]] | None = None) -> list[str]:
        history = "".join(
            f"\nYou previously proposed: {plan}\nThe user rejected it because: {reason or 'no reason given'}"
            for plan, reason in (rejected or [])
        )
        response = self._ask(f"""Create an implementation plan for this GitHub issue.
Issue title: {issue.title}
Issue body: {issue.body}
Repository files:
{snapshot[:4000]}

Contents of the files most relevant to the issue:
{sources[:12000]}{history}
{'Propose a genuinely different approach that addresses the rejection.' if history else ''}
Write each step as "<exact path>: <what to change in it>", one file per step. Read the
contents above and decide which file; do not hedge between candidates. Include every file the change needs — for a
new option or variant that means the file which lists or registers it, not only the new file.
Do not include steps that change no file, such as running, testing, verifying or committing.
Return exactly {{"plan":["step", "step"]}} with 1–5 steps.""", valid_plan)
        return response["plan"]

    def _changes(self, workspace: Path, issue: Issue, plan: list[str], snapshot: str, sources: str,
                 feedback: str = "", already: frozenset[str] = frozenset()) -> list[dict]:
        """Ask for the change set, validated inside the retry loop.

        An unmatched anchor or a skipped plan step is as useless as a malformed reply, and as
        likely to succeed on another attempt, so both are rejected there with a reason the next
        attempt is told about. `already` holds paths changed by an earlier pass, which count
        towards covering the plan when a debug or revision pass touches only one file.
        """
        def shape(response: dict) -> bool:
            if not valid_changes(response):
                return False
            paths = set(already) | {change["path"] for change in response["changes"]}
            reason = problem(workspace, response["changes"]) or uncovered(plan, paths)
            if reason:
                raise ValueError(reason)
            return True

        steps = "\n".join(f"{number}. {step}" for number, step in enumerate(plan, 1))
        extra = f"\n{feedback[-5000:]}\n" if feedback else ""
        response = self._ask(f"""Implement this issue in the cloned repository.
Issue: {issue.title}
{issue.body}

Approved plan. Implement every step; a skipped step is a missing part of the change:
{steps}

Repository files: {snapshot[:3000]}

Current contents of existing files:
{sources[:16000]}
{extra}
Return exactly {{"changes":[ ... ],"summary":"short"}} with 1–8 entries, each in ONE of two forms.

To EDIT a file that already exists, return an anchored edit and nothing else:
  {{"path":"relative/path", "find":"<exact text copied from the file above>", "replace":"<new text>"}}
"find" must be copied character for character from the contents above and must appear exactly
once in that file. Keep it to the few lines you are changing plus just enough surrounding text
to be unique. Never send the whole file this way.

To CREATE a file that does not exist yet:
  {{"path":"relative/path", "content":"complete new file content"}}

Use an edit for every existing file. Returning a whole existing file risks dropping imports or
truncating it, which deletes working code. Do not include a test command.""", shape)
        return response["changes"]

    def _review(self, issue: Issue, patch: str, verification: str) -> dict:
        """Ask the model to review its own diff before the pull request is opened.

        A self-review catches the failure the test suite cannot: a change that passes because
        it does something other than what the issue asked for. The verdict never blocks the
        pull request — it is written into the body so the human reviewer sees it first.
        """
        response = self._ask(f"""Review this diff as a critical reviewer.
Issue: {issue.title}\n{issue.body}
Verification result: {verification[-2000:]}
Diff:
{patch}
Judge only what the diff shows. Does it address the issue, and does it break or delete anything?
Return exactly {{"verdict":"approve"|"concerns","summary":"one sentence","findings":["finding"]}}.
Use "concerns" only for a problem that should stop this merging: the issue not fully addressed,
something broken or deleted, or changes unrelated to the issue. Re-indenting or reformatting
existing lines the issue did not require is an unrelated change. Do not raise concerns about
missing tests, documentation, or style. Return at most 5 findings.""")
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

    @staticmethod
    def _sources(workspace: Path, issue: Issue, plan: list[str] | None = None) -> str:
        """Select file contents for the coder, rebuilt each pass so it sees files as they now are.

        The approved plan is part of the query: a path the plan names scores above everything
        else, so the file a step says to edit is guaranteed to be in front of the coder. Ranking
        on the issue alone let a large file the plan depends on lose its place to small files
        that merely share vocabulary with the issue — and a coder that cannot see a file can
        only invent the text it is asked to anchor an edit to.
        """
        return source_context(workspace, "\n".join([issue.title, issue.body, *(plan or [])]))

    def _verify(self, run: Run, workspace: Path, issue: Issue, snapshot: str, authored: dict) -> str | None:
        """Verify the change, allowing one debugging pass. Returns None if the run has failed."""
        for attempt in range(2):
            self._transition(run, RunState.VERIFYING, "Running locally discovered verification commands.")
            try:
                verification = verify(workspace)
            except Exception as error:
                verification = str(error)
                if attempt:
                    self._transition(run, RunState.FAILED, f"Verification failed after one debug attempt: {verification[-500:]}")
                    return None
                self._transition(run, RunState.IMPLEMENTING, "Verification failed; requesting one constrained debugging pass.")
                feedback = "The previous attempt failed verification. Fix only the cause:\n" + verification
                retried = apply_changes(workspace, self._changes(
                    workspace, issue, run.plan, snapshot, self._sources(workspace, issue, run.plan), feedback, frozenset(authored)))
                authored.update(dict.fromkeys(retried))
                self._transition(run, RunState.IMPLEMENTING, f"Debug pass changed: {', '.join(retried)}")
                continue
            self._transition(run, RunState.VERIFYING, outcome(verification), detail=True)
            return verification
        return None

    def _reviewed(self, run: Run, issue: Issue, workspace: Path, authored: dict, verification: str) -> dict:
        paths = list(authored)
        self._transition(run, RunState.REVIEWING, f"Reviewing the diff. {changed_files(workspace, paths)}")
        try:
            review = self._review(issue, diff(workspace, paths), verification)
        except Exception as error:
            # A review that cannot be produced must not discard a change that already passed
            # its tests; the pull request opens with the failure recorded in its place.
            review = {"verdict": "unclear", "summary": f"The review could not be produced: {error}", "findings": []}
        self._transition(run, RunState.REVIEWING, f"Review verdict: {review['verdict']}. {review['summary']}")
        for finding in review["findings"]:
            self._transition(run, RunState.REVIEWING, f"· {finding}", detail=True)
        return review

    def run(self, repository_url: str, issue_number: int, approve: ApprovalHandler) -> Run:
        """Execute one run, recording any failure as a persisted state rather than a traceback."""
        run = Run(repository_url=repository_url, issue_number=issue_number)
        self.store.save(run)
        self._active = run
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
            run.plan = self._plan(issue, snapshot, sources, rejected)
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
        changes = self._changes(workspace, issue, run.plan, snapshot, self._sources(workspace, issue, run.plan))
        # Accumulated across the debug pass: verification installs dependencies into the clone,
        # so only paths DevPilot authored may be staged, and a debug pass must not drop the first.
        authored = dict.fromkeys(apply_changes(workspace, changes))
        self._transition(run, RunState.IMPLEMENTING, f"Changed: {', '.join(authored)}")
        verification = self._verify(run, workspace, issue, snapshot, authored)
        if verification is None:
            return run
        review = self._reviewed(run, issue, workspace, authored, verification)
        if review["verdict"] == "concerns":
            # The review is acted on once rather than merely attached: an incomplete change
            # that reaches a pull request with its own reviewer's objection stapled to it helps
            # nobody. Once, because a reviewer that is never satisfied must not loop forever.
            self._transition(run, RunState.IMPLEMENTING, "The review raised concerns; revising the change once.")
            feedback = ("A review of this change raised the concerns below. Address them and change nothing else:\n"
                        + "\n".join(f"- {item}" for item in review["findings"] or [review["summary"]]))
            try:
                revision = self._changes(workspace, issue, run.plan, snapshot, self._sources(workspace, issue, run.plan),
                                         feedback, frozenset(authored))
            except OpenRouterError as error:
                self._transition(run, RunState.IMPLEMENTING,
                                 f"No revision could be produced; keeping the reviewed change. ({error})", detail=True)
            else:
                retried = apply_changes(workspace, revision)
                authored.update(dict.fromkeys(retried))
                self._transition(run, RunState.IMPLEMENTING, f"Revision changed: {', '.join(retried)}")
                verification = self._verify(run, workspace, issue, snapshot, authored)
                if verification is None:
                    return run
                review = self._reviewed(run, issue, workspace, authored, verification)
        run.review = review
        self.store.save(run)
        paths = list(authored)
        base = default_branch(workspace)
        self._transition(run, RunState.CREATING_DRAFT_PR, "Committing, pushing, and creating a GitHub draft PR.")
        commit_and_push(workspace, branch, f"feat: address issue #{issue_number}", self.github_token, paths)
        body = f"Closes #{issue_number}\n\n{self._review_body(review)}\n\nDevPilot verification:\n{verification[-2000:]}"
        pr_url = self.github.create_draft_pr(owner, repository, f"Draft: {issue.title}", body, branch, base)
        self._transition(run, RunState.COMPLETED, f"Draft PR created: {pr_url}")
        return run
