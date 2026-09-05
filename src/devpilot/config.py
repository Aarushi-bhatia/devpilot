from __future__ import annotations

import os
from pathlib import Path


DEFAULT_CONFIG = """# DevPilot user configuration
# Credentials belong in ~/.dev-pilot/.env, never in a repository.
# This is deliberately fixed to OpenRouter's zero-cost router.
model = "openrouter/free"
max_debug_attempts = 1

[execution]
# Test commands are discovered from common project files and run inside a clone.
enabled = true
timeout_seconds = 120
"""

ENV_TEMPLATE = """# Required for live DevPilot runs. Keep this file private.
GITHUB_TOKEN=
OPENROUTER_API_KEY=
"""


def home_dir() -> Path:
    """Return DevPilot's user-owned application directory."""
    return Path(os.environ.get("DEV_PILOT_HOME", Path.home() / ".dev-pilot")).expanduser()


def config_path() -> Path:
    return home_dir() / "config.toml"


def database_path() -> Path:
    return home_dir() / "runs.sqlite3"


def env_path() -> Path:
    return home_dir() / ".env"


def initialize() -> tuple[Path, bool]:
    """Create the DevPilot home and default config, without overwriting user data."""
    directory = home_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = config_path()
    created = not path.exists()
    if created:
        path.write_text(DEFAULT_CONFIG, encoding="utf-8")
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
