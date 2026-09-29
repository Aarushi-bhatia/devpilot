from __future__ import annotations

import os
from pathlib import Path


ENV_TEMPLATE = """# Required for live DevPilot runs. Keep this file private.
GITHUB_TOKEN=
OPENROUTER_API_KEY=
"""


def home_dir() -> Path:
    """Return DevPilot's user-owned application directory."""
    return Path(os.environ.get("DEV_PILOT_HOME", Path.home() / ".dev-pilot")).expanduser()


def database_path() -> Path:
    return home_dir() / "runs.sqlite3"


def env_path() -> Path:
    return home_dir() / ".env"


def initialize() -> tuple[Path, bool]:
    """Create the DevPilot home and a private secrets template, without overwriting user data.

    There is deliberately no settings file: the model is pinned to a zero-cost router and the
    debug budget is fixed, so exposing them as configuration would only invite breaking those
    guarantees. Secrets are the one thing a user must supply, and they live in .env alone.
    """
    directory = home_dir()
    created = not directory.exists()
    directory.mkdir(parents=True, exist_ok=True)
    secrets = env_path()
    if not secrets.exists():
        secrets.write_text(ENV_TEMPLATE, encoding="utf-8")
        secrets.chmod(0o600)
    return directory, created


def load_secrets() -> dict[str, str]:
    """Load a minimal dotenv file without adding a runtime dependency."""
    values: dict[str, str] = {}
    if env_path().exists():
        for line in env_path().read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip("\"'")
    return {**values, **{key: value for key, value in os.environ.items() if value}}


def workspaces_dir() -> Path:
    path = home_dir() / "workspaces"
    path.mkdir(parents=True, exist_ok=True)
    return path
