from __future__ import annotations

import base64
import os
import subprocess
from pathlib import Path

from . import sandbox


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


SOURCE_SUFFIXES = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".rb", ".php", ".java", ".cs",
    ".c", ".h", ".cpp", ".sh", ".json", ".toml", ".yaml", ".yml", ".md", ".txt", ".cfg",
}


def source_context(workspace: Path, issue_text: str, budget: int = 40_000, per_file: int = 12_000) -> str:
    """Return the current contents of files the coder may be asked to rewrite.

    The coder returns complete file replacements, so without the existing text it silently
    deletes everything it did not think to write. Files named in the issue come first, so
    they survive the budget even in a large repository.
    """
    names = command(["git", "ls-files"], workspace).splitlines()
    mentioned = [n for n in names if n in issue_text or Path(n).name in issue_text]
    sections, used = [], 0
    for name in mentioned + [n for n in names if n not in mentioned]:
        path = workspace / name
        if path.suffix.lower() not in SOURCE_SUFFIXES or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if len(text) > per_file or used + len(text) > budget:
            continue
        sections.append(f"--- {name} ---\n{text}")
        used += len(text)
    return "\n\n".join(sections) if sections else "(no readable source files)"


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


def changed_files(workspace: Path, paths: list[str] | None = None) -> str:
    """Report modified and newly added files; plain diff --stat omits untracked additions.

    Scoped to the generated paths when given, so dependencies installed during verification
    do not drown the review line in noise.
    """
    return command(["git", "status", "--short", "--"] + (paths or ["."]), workspace) or "No changes"


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

    Prefers a container, where dependencies can be installed safely and the suite cannot
    reach the host. Without a container runtime it falls back to already-installed host
    tooling, which cannot install anything and so usually skips.

    A missing toolchain is an environment gap, not a defect in the generated change, so it
    is reported as a skip instead of a failure that would trigger a pointless debug pass.
    """
    command(["git", "diff", "--check"], workspace)
    if sandbox.available():
        try:
            return sandbox.verify(workspace, os.getuid(), os.getgid())
        except sandbox.SandboxUnavailable as error:
            # The change was never executed, so this is a skip, not a failing suite.
            return SKIPPED.format(f"sandboxed verification did not run — {error}")
    if sandbox.installed():
        return SKIPPED.format("Docker is installed but its daemon is not responding")
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


def commit_and_push(workspace: Path, branch: str, message: str, github_token: str, paths: list[str]) -> None:
    """Commit only the paths DevPilot generated.

    Verification installs the repository's dependencies into the clone, so `git add --all`
    would sweep node_modules, __pycache__ and build output into the pull request whenever the
    target repository does not happen to ignore them. Staging the generated paths explicitly
    keeps the diff to what was actually authored, whatever the suite left behind.
    """
    command(["git", "add", "--"] + paths, workspace)
    command(["git", "commit", "-m", message], workspace)
    command(["git", "push", "-u", "origin", branch], workspace, extra_env=github_auth(github_token))
