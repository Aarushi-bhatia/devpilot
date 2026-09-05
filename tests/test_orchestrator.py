from pathlib import Path

import pytest

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


def test_verify_skips_instead_of_failing_when_dependencies_are_absent(tmp_path: Path) -> None:
    """A missing toolchain must not be reported as a failing verification."""
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
