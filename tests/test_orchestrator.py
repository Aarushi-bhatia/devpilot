from pathlib import Path

import pytest

from devpilot import sandbox
from devpilot.openrouter import OpenRouterError, json_response
from devpilot.workspace import apply_changes, changed_files, command, source_context, verify


def repository(tmp_path: Path) -> Path:
    """Create a minimal committed git repository to run workspace helpers against."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    command(["git", "init", "--initial-branch", "main"], workspace)
    command(["git", "config", "user.name", "DevPilot Test"], workspace)
    command(["git", "config", "user.email", "test@example.com"], workspace)
    (workspace / "README.md").write_text("seed\n")
    command(["git", "add", "--all"], workspace)
    command(["git", "commit", "-m", "seed"], workspace)
    return workspace


def test_changes_cannot_escape_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(Exception, match="Unsafe"):
        apply_changes(workspace, [{"path": "../outside.py", "content": "bad"}])


def test_changes_write_inside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    assert apply_changes(workspace, [{"path": "src/app.py", "content": "print('ok')"}]) == ["src/app.py"]
    assert (workspace / "src/app.py").read_text() == "print('ok')"


def test_verify_skips_instead_of_failing_when_dependencies_are_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing toolchain must not be reported as a failing verification."""
    monkeypatch.setattr(sandbox, "available", lambda: False)
    workspace = repository(tmp_path)
    (workspace / "package.json").write_text('{"name":"x","scripts":{"test":"jest"}}\n')
    assert "Verification skipped" in verify(workspace)


def test_changed_files_reports_newly_added_files(tmp_path: Path) -> None:
    """git diff --stat omits untracked additions, which are most of what DevPilot writes."""
    workspace = repository(tmp_path)
    apply_changes(workspace, [{"path": "LICENSE", "content": "MIT\n"}])
    assert "LICENSE" in changed_files(workspace)


def test_json_response_extracts_object_from_surrounding_prose() -> None:
    """Some free models wrap the object in commentary or a Markdown fence."""
    assert json_response('Sure!\n```json\n{"plan":["a","b"]}\n```\nHope that helps.') == {"plan": ["a", "b"]}


def test_json_response_rejects_a_non_json_reply() -> None:
    """The free router sometimes answers with a safety classifier instead of a chat model."""
    with pytest.raises(OpenRouterError):
        json_response("User Safety: safe")


def test_source_context_includes_contents_and_prioritises_the_named_file(tmp_path: Path) -> None:
    """The coder rewrites whole files, so it must receive their current contents."""
    workspace = repository(tmp_path)
    (workspace / "src").mkdir()
    (workspace / "src/utils.js").write_text("function existing() { return 1; }\n")
    (workspace / "src/other.js").write_text("function other() { return 2; }\n")
    command(["git", "add", "--all"], workspace)
    command(["git", "commit", "-m", "sources"], workspace)
    context = source_context(workspace, "Add a helper to src/utils.js please")
    assert "function existing()" in context
    assert context.index("src/utils.js") < context.index("src/other.js")


def test_sandbox_selects_the_ecosystem_matching_the_repository(tmp_path: Path) -> None:
    workspace = repository(tmp_path)
    assert sandbox.recipe(workspace) is None
    (workspace / "package.json").write_text("{}\n")
    image, script = sandbox.recipe(workspace)
    assert "node" in image and "npm test" in script


def test_sandbox_never_exposes_the_host_environment_or_filesystem(tmp_path: Path) -> None:
    """The container must not inherit host env vars, which is where the GitHub token lives."""
    argv = sandbox.arguments(tmp_path, "node:22-slim", "npm test", 501, 20)
    passed = [argv[i + 1] for i, item in enumerate(argv) if item == "--env"]
    assert passed == ["HOME=/tmp", "CI=true"]
    mounts = [argv[i + 1] for i, item in enumerate(argv) if item == "--volume"]
    assert mounts == [f"{tmp_path}:/work"]
    assert "--cap-drop" in argv and "no-new-privileges" in argv
    assert argv[argv.index("--user") + 1] == "501:20"


def test_commit_stages_only_generated_paths(tmp_path: Path) -> None:
    """Verification installs dependencies into the clone; they must not enter the commit."""
    workspace = repository(tmp_path)
    apply_changes(workspace, [{"path": "LICENSE", "content": "MIT\n"}])
    (workspace / "node_modules").mkdir()
    (workspace / "node_modules/big.js").write_text("// installed during verification\n")
    command(["git", "add", "--", "LICENSE"], workspace)
    command(["git", "commit", "-m", "generated"], workspace)
    committed = command(["git", "show", "--name-only", "--format=", "HEAD"], workspace).split()
    assert committed == ["LICENSE"]


def test_sandbox_distinguishes_a_failing_suite_from_a_broken_container() -> None:
    """A container that never started says nothing about the generated change."""
    assert not issubclass(sandbox.SandboxUnavailable, sandbox.SandboxError)


def test_sandbox_finds_docker_desktop_when_it_is_not_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox.shutil, "which", lambda _: None)
    monkeypatch.setattr(sandbox, "FALLBACK_BINARIES", (Path(__file__),))
    assert sandbox.executable() == str(Path(__file__))
    assert sandbox.installed()
