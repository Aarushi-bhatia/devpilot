from __future__ import annotations

import re

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .config import database_path, initialize, load_secrets, workspaces_dir
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
    console.print(f"[bold cyan]{event.state.value:>20}[/]  {event.message}")


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

    def approve(workflow_run: Run) -> bool:
        console.print("\n[bold]Implementation plan[/]")
        for number, step in enumerate(workflow_run.plan, 1):
            console.print(f"  {number}. {step}")
        console.print("\n[dim]Approval is required before implementation.[/]")
        return typer.confirm("Approve this plan?", default=False)

    secrets = load_secrets()
    workflow_run = LiveOrchestrator(
        store(), display_event, secrets.get("GITHUB_TOKEN", ""), secrets.get("OPENROUTER_API_KEY", ""), workspaces_dir(),
    ).run(repository_url, issue, approve)
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
    for event in workflow_run.events:
        console.print(f"[cyan]{event.at}[/] [bold]{event.state.value}[/] — {event.message}")


if __name__ == "__main__":
    app()
