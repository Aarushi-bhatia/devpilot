from __future__ import annotations

import base64
import os
import subprocess
from pathlib import Path


class WorkspaceError(RuntimeError):
    pass


def command(arguments: list[str], cwd: Path, timeout: int = 120, extra_env: dict[str, str] | None = None) -> str:
    try:
        result = subprocess.run(
            arguments, cwd=cwd, text=True, capture_output=True, timeout=timeout, check=False,
            env={**os.environ, **(extra_env or {})},
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise WorkspaceError(f"Could not run {' '.join(arguments)}: {error}") from error
    output = (result.stdout + result.stderr).strip()
    if result.returncode:
        raise WorkspaceError(f"{' '.join(arguments)} failed ({result.returncode}):\n{output[-6000:]}")
    return output


def github_auth(token: str) -> dict[str, str]:
    """Pass the PAT to Git without putting it in a URL, command, or remote config."""
    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {encoded}",
    }


def clone(url: str, destination: Path, github_token: str) -> None:
    if destination.exists():
        raise WorkspaceError(f"Workspace already exists: {destination}")
    command(["git", "clone", "--depth", "1", url, str(destination)], destination.parent, extra_env=github_auth(github_token))


def default_branch(workspace: Path) -> str:
    return command(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], workspace).removeprefix("origin/")


def repository_snapshot(workspace: Path) -> str:
    names = command(["git", "ls-files"], workspace).splitlines()[:180]
    snippets: list[str] = []
    for name in names:
        path = workspace / name
        if path.stat().st_size > 12_000 or path.suffix in {".lock", ".png", ".jpg", ".gif", ".pdf"}:
            continue
        if name.lower().startswith(("readme", "contributing")) or name in {"pyproject.toml", "package.json", "go.mod", "cargo.toml"}:
            snippets.append(f"--- {name} ---\n{path.read_text(encoding='utf-8', errors='replace')[:6000]}")
    return "Files:\n" + "\n".join(names) + "\n\nKey files:\n" + "\n\n".join(snippets)


def apply_changes(workspace: Path, changes: list[dict]) -> list[str]:
    if not 1 <= len(changes) <= 8:
        raise WorkspaceError("DevPilot accepts between 1 and 8 generated file changes per run.")
    changed: list[str] = []
    root = workspace.resolve()
    for change in changes:
        path_value, content = change.get("path"), change.get("content")
        if not isinstance(path_value, str) or not isinstance(content, str) or len(content.encode()) > 100_000:
            raise WorkspaceError("Generated changes must have a relative path and text under 100 KB.")
        target = (workspace / path_value).resolve()
        if root not in target.parents or ".git" in target.parts:
            raise WorkspaceError(f"Unsafe generated path rejected: {path_value}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        changed.append(path_value)
    return changed


def create_branch(workspace: Path, branch: str) -> None:
    command(["git", "checkout", "-b", branch], workspace)


def changed_files(workspace: Path) -> str:
    """Report modified and newly added files; plain diff --stat omits untracked additions."""
    return command(["git", "status", "--short"], workspace) or "No changes"


SKIPPED = "Verification skipped: {}. This draft PR carries no test signal; review the diff manually."


def usable(arguments: list[str], workspace: Path) -> bool:
    """Probe whether a verification toolchain is actually installed and runnable here."""
    try:
        command(arguments, workspace, timeout=60)
    except WorkspaceError:
        return False
    return True


def verify(workspace: Path) -> str:
    """Run only fixed, locally discovered test commands; never model-provided shell text.

    A missing toolchain is an environment gap, not a defect in the generated change, so it
    is reported as a skip instead of a failure that would trigger a pointless debug pass.
    """
    command(["git", "diff", "--check"], workspace)
    if (workspace / "pyproject.toml").exists() or (workspace / "pytest.ini").exists():
        if not usable(["python3", "-m", "pytest", "--version"], workspace):
            return SKIPPED.format("pytest is not installed")
        return command(["python3", "-m", "pytest", "-q"], workspace)
    if (workspace / "package.json").exists():
        if not usable(["npm", "--version"], workspace):
            return SKIPPED.format("npm is not installed")
        if not (workspace / "node_modules").is_dir():
            return SKIPPED.format("JavaScript dependencies are not installed (no node_modules)")
        return command(["npm", "test", "--", "--watch=false"], workspace)
    if (workspace / "go.mod").exists():
        if not usable(["go", "version"], workspace):
            return SKIPPED.format("Go is not installed")
        return command(["go", "test", "./..."], workspace)
    if (workspace / "Cargo.toml").exists():
        if not usable(["cargo", "--version"], workspace):
            return SKIPPED.format("Cargo is not installed")
        return command(["cargo", "test"], workspace)
    return "No supported test configuration found; whitespace validation passed."


def commit_and_push(workspace: Path, branch: str, message: str, github_token: str) -> None:
    command(["git", "add", "--all"], workspace)
    command(["git", "commit", "-m", message], workspace)
    command(["git", "push", "-u", "origin", branch], workspace, extra_env=github_auth(github_token))
