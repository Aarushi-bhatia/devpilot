"""Run a cloned repository's own test suite inside a disposable container.

Verifying generated code means executing the target repository's code: its test script, its
conftest, and — if dependencies are installed — the postinstall hooks of every package it
depends on. On the host that code runs as the user, with the user's environment, next to the
user's credentials. In a container it gets a throwaway filesystem, no host environment, and
no way back out, which is what makes installing dependencies safe enough to be worth doing.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

# Each ecosystem installs and tests in one shell invocation. Install failures are not fatal:
# a suite that runs without its optional dependencies still tells us more than no suite.
RECIPES: list[tuple[str, str, str]] = [
    ("pyproject.toml", "python:3.12-slim",
     "pip install --quiet --disable-pip-version-check -e '.[dev]' || pip install --quiet -e . || true; python -m pytest -q"),
    ("pytest.ini", "python:3.12-slim",
     "pip install --quiet --disable-pip-version-check -r requirements.txt || true; python -m pytest -q"),
    ("package.json", "node:22-slim", "npm ci --silent || npm install --silent; npm test"),
    ("go.mod", "golang:1.23-alpine", "go test ./..."),
    ("Cargo.toml", "rust:1-slim", "cargo test"),
]


class SandboxError(RuntimeError):
    """The suite ran inside the container and reported failure — a defect in the change."""


class SandboxUnavailable(RuntimeError):
    """The container itself could not run. Nothing was learned about the generated change,
    so this must never be fed back to the model as though its code were at fault."""


# Docker Desktop for macOS installs its CLI here and does not always add it to PATH, which
# would otherwise make DevPilot silently fall back to unsandboxed verification.
FALLBACK_BINARIES = (
    Path.home() / ".docker/bin/docker",
    Path("/usr/local/bin/docker"),
    Path("/opt/homebrew/bin/docker"),
)


def executable() -> str | None:
    """Locate the container runtime, looking beyond PATH before giving up."""
    found = shutil.which("docker")
    if found:
        return found
    return next((str(path) for path in FALLBACK_BINARIES if path.exists()), None)


def installed() -> bool:
    """Return whether a container runtime exists at all, regardless of daemon state."""
    return executable() is not None


def available() -> bool:
    """Return whether a usable container runtime is present and its daemon is responding."""
    binary = executable()
    if binary is None:
        return False
    try:
        return subprocess.run([binary, "info"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def recipe(workspace: Path) -> tuple[str, str] | None:
    """Return the (image, script) pair for the first ecosystem this repository matches."""
    for marker, image, script in RECIPES:
        if (workspace / marker).exists():
            return image, script
    return None


def arguments(workspace: Path, image: str, script: str, uid: int, gid: int, binary: str = "docker") -> list[str]:
    """Build the container invocation. Every flag here is load-bearing, so they are listed
    with their reason rather than folded into one line."""
    return [
        binary, "run", "--rm",
        # Only the clone is visible. The host filesystem, and ~/.dev-pilot/.env with it,
        # is not mounted and therefore cannot be read by anything running inside.
        "--volume", f"{workspace}:/work",
        "--workdir", "/work",
        # No -e flags: the host environment is not inherited, so no token reaches the container.
        "--env", "HOME=/tmp",
        "--env", "CI=true",
        # Write as the invoking user so the clone does not come back owned by root.
        "--user", f"{uid}:{gid}",
        # A test suite needs no privileges of any kind.
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        # Bound the blast radius of a runaway or hostile suite.
        "--memory", "2g",
        "--pids-limit", "512",
        "--cpus", "2",
        image, "sh", "-lc", script,
    ]


def verify(workspace: Path, uid: int, gid: int, timeout: int = 600) -> str:
    """Install dependencies and run the repository's suite inside a container.

    Returns the captured output on success. Raises SandboxError when the suite fails, which
    the caller treats as a genuine defect in the generated change — unlike a missing
    toolchain on the host, which is not the model's fault and is reported as a skip.
    """
    selected = recipe(workspace)
    if selected is None:
        return "No supported test configuration found."
    image, script = selected
    try:
        result = subprocess.run(
            arguments(workspace, image, script, uid, gid, executable() or "docker"),
            text=True, capture_output=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SandboxUnavailable(f"the container could not run ({error})") from error
    output = (result.stdout + result.stderr).strip()
    if result.returncode == 125:
        # Docker's own exit code for "the container never started": bad image, pull failure,
        # daemon gone. The generated change was never executed.
        raise SandboxUnavailable(f"the container never started:\n{output[-2000:]}")
    if result.returncode:
        raise SandboxError(f"Sandboxed tests failed ({result.returncode}):\n{output[-6000:]}")
    return f"Sandboxed tests passed in {image}:\n{output[-4000:]}"
