from __future__ import annotations

import re
import shutil

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .config import database_path, initialize, load_secrets, reclaimable, workspaces_dir
from .models import Event, Run
from .orchestrator import LiveOrchestrator
from .store import RunStore

app = typer.Typer(no_args_is_help=True, help="DevPilot — transparent terminal-first engineering automation.")
console = Console()
GITHUB_REPOSITORY = re.compile(r"^https://github\.com/[\w.-]+/[\w.-]+/?$")


def store() -> RunStore:
    initialize()
    return RunStore(database_path())


def display_event(event: Event) -> None:
    if event.detail:
        console.print(f"{'':>20}  [dim]{event.message}[/]")
    else:
        console.print(f"[bold cyan]{event.state.value:>20}[/]  {event.message}")


class Waiting:
    """A live spinner with elapsed seconds while a model call is in flight.

    Free models routinely take a minute or more, and a screen that prints nothing for that
    long is indistinguishable from a hang. Rich renders events printed meanwhile above it.
    """

    def __init__(self) -> None:
        self.status = None

    def __call__(self, elapsed: float | None) -> None:
        if elapsed is None:
            if self.status is not None:
                self.status.stop()
                self.status = None
            return
        text = f"[dim]waiting for the model… {int(elapsed)}s[/]"
        if self.status is None:
            self.status = console.status(text, spinner="dots")
            self.status.start()
        else:
            self.status.update(text)


@app.command()
def init() -> None:
    """Create DevPilot's user directory and a private .env template."""
    directory, created = initialize()
    message = "Created. Add your GITHUB_TOKEN and OPENROUTER_API_KEY to .env." if created else "Already exists; left unchanged."
    console.print(Panel(f"[bold]{directory}[/]\n{message}", title="DevPilot initialized", border_style="green"))


@app.command()
def run(
    repository_url: str = typer.Argument(..., help="GitHub repository URL, e.g. https://github.com/org/repo"),
    issue: int = typer.Option(..., "--issue", min=1, help="GitHub issue number."),
) -> None:
    """Run the live, free-only Issue → Plan → Approval → draft-PR workflow."""
    repository_url = repository_url.rstrip("/")
    if not GITHUB_REPOSITORY.fullmatch(repository_url):
        raise typer.BadParameter("Provide a GitHub repository URL: https://github.com/owner/repository")

    def approve(workflow_run: Run) -> bool | str:
        console.print("\n[bold]Implementation plan[/]")
        for number, step in enumerate(workflow_run.plan, 1):
            console.print(f"  {number}. {step}")
        console.print("\n[dim]Nothing has been written yet. [y] approve  [r] re-plan  [N] reject[/]")
        answer = typer.prompt("Approve this plan? [y/r/N]", default="N", show_default=False).strip().lower()
        if answer.startswith("y"):
            return True
        if answer.startswith("r"):
            return typer.prompt("What should change?").strip() or "The approach was rejected without detail."
        return False

    secrets = load_secrets()
    waiting = Waiting()
    try:
        workflow_run = LiveOrchestrator(
            store(), display_event, secrets.get("GITHUB_TOKEN", ""), secrets.get("OPENROUTER_API_KEY", ""),
            workspaces_dir(), on_wait=waiting,
        ).run(repository_url, issue, approve)
    finally:
        waiting(None)  # never leave a spinner running over the summary or a traceback
    color = "green" if workflow_run.state.value == "completed" else "yellow"
    console.print(Panel(f"Run ID: [bold]{workflow_run.id}[/]\nFinal state: [bold]{workflow_run.state.value}[/]", title="DevPilot", border_style=color))


@app.command()
def history(limit: int = typer.Option(10, min=1, max=100)) -> None:
    """List recent DevPilot runs."""
    runs = store().recent(limit)
    if not runs:
        console.print("No runs yet. Start one with: devpilot run https://github.com/owner/repo --issue 1")
        return
    table = Table(title="Recent DevPilot runs")
    table.add_column("Run ID", style="cyan")
    table.add_column("Issue")
    table.add_column("State")
    table.add_column("Repository")
    for item in runs:
        table.add_row(item.id, f"#{item.issue_number}", item.state.value, item.repository_url)
    console.print(table)


@app.command()
def show(run_id: str) -> None:
    """Show the complete state timeline for one run."""
    workflow_run = store().get(run_id)
    if workflow_run is None:
        raise typer.BadParameter(f"No saved run named {run_id!r}.")
    console.print(Panel(f"{workflow_run.repository_url}\nIssue #{workflow_run.issue_number}", title=f"Run {workflow_run.id}"))
    if workflow_run.review:
        colour = {"approve": "green", "concerns": "yellow"}.get(workflow_run.review.get("verdict"), "dim")
        findings = "\n".join(f"· {item}" for item in workflow_run.review.get("findings", []))
        console.print(Panel(f"{workflow_run.review.get('summary', '')}\n{findings}".strip(),
                            title=f"Review — {workflow_run.review.get('verdict')}", border_style=colour))
    for event in workflow_run.events:
        if event.detail:
            console.print(f"{'':>32} [dim]{event.message}[/]")
        else:
            console.print(f"[cyan]{event.at}[/] [bold]{event.state.value}[/] — {event.message}")


@app.command()
def clean(
    keep: int = typer.Option(2, min=0, help="Keep this many of the most recent runs' workspaces."),
    force: bool = typer.Option(False, "--force", help="Delete without confirming."),
) -> None:
    """Delete cloned workspaces, which grow once verification installs dependencies."""
    recent = {item.id for item in store().recent(keep)} if keep else set()
    targets = reclaimable(recent)
    if not targets:
        console.print("Nothing to clean.")
        return
    total = sum(size for _, size in targets)
    for path, size in targets:
        console.print(f"  {path.name}  [dim]{size / 1_000_000:.1f} MB[/]")
    console.print(f"\n{len(targets)} workspace(s), [bold]{total / 1_000_000:.1f} MB[/]. Run history is kept.")
    if not force and not typer.confirm("Delete these?", default=False):
        console.print("Left unchanged.")
        return
    for path, _ in targets:
        shutil.rmtree(path, ignore_errors=True)
    console.print(f"[green]Reclaimed {total / 1_000_000:.1f} MB.[/]")


if __name__ == "__main__":
    app()
