from pathlib import Path

import pytest

from devpilot import sandbox
from devpilot.openrouter import OpenRouterError, json_response
from devpilot.orchestrator import LiveOrchestrator
from devpilot.workspace import (
    applicable, apply_changes, changed_files, command, diff, keywords, source_context, verify,
)


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
    assert (workspace / "src/app.py").read_text() == "print('ok')\n", "a created file ends with a newline"


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


def test_diff_includes_files_that_are_not_yet_tracked(tmp_path: Path) -> None:
    """Most of what DevPilot writes is new, and plain git diff would show none of it."""
    workspace = repository(tmp_path)
    apply_changes(workspace, [{"path": "LICENSE", "content": "MIT License\n"}])
    patch = diff(workspace, ["LICENSE"])
    assert "LICENSE" in patch and "+MIT License" in patch


def test_review_body_marks_a_self_review_as_unreliable() -> None:
    """The reviewer is the author, so the pull request must say so."""
    body = LiveOrchestrator._review_body(
        {"verdict": "concerns", "summary": "Removes an export.", "findings": ["isPalindrome dropped"]}
    )
    assert "Concerns raised" in body and "isPalindrome dropped" in body
    assert "same model that wrote the change" in body


def test_relevance_beats_alphabetical_order_when_the_budget_is_tight(tmp_path: Path) -> None:
    """A natural issue names no paths, so ranking is what puts the right files in context."""
    workspace = repository(tmp_path)
    (workspace / "themes").mkdir()
    (workspace / "themes/nord.tcss").write_text("$bg-color: #242933;\n")
    (workspace / "aaa_unrelated.py").write_text("# " + "filler " * 200 + "\n")
    (workspace / "settings.py").write_text("THEMES = ['nord']\n")
    command(["git", "add", "--all"], workspace)
    command(["git", "commit", "-m", "sources"], workspace)

    context = source_context(workspace, "Add a Solarized Dark theme", budget=400)
    assert "nord.tcss" in context, "a theme file must survive a tight budget"
    assert "aaa_unrelated.py" not in context, "alphabetical order must not win"


def test_an_explicitly_named_file_outranks_everything(tmp_path: Path) -> None:
    workspace = repository(tmp_path)
    (workspace / "one.py").write_text("# theme theme theme\n")
    (workspace / "two.py").write_text("# unrelated\n")
    command(["git", "add", "--all"], workspace)
    command(["git", "commit", "-m", "sources"], workspace)
    context = source_context(workspace, "Fix the bug in two.py, something about theme handling")
    assert context.index("two.py") < context.index("one.py")


def test_common_words_do_not_drive_the_ranking() -> None:
    assert keywords("Add a new file to the project") == {"project"}


def test_an_anchored_edit_leaves_the_rest_of_the_file_untouched(tmp_path: Path) -> None:
    """The whole point: the model sends only the fragment it changes, so it cannot mangle the rest."""
    workspace = repository(tmp_path)
    original = "import os\n\nOPTIONS = [\n    'a',\n    'b',\n]\n\ndef untouched():\n    return 1\n"
    (workspace / "settings.py").write_text(original)
    apply_changes(workspace, [{"path": "settings.py", "find": "    'b',\n", "replace": "    'b',\n    'c',\n"}])
    updated = (workspace / "settings.py").read_text()
    assert "'c'," in updated
    assert "import os" in updated and "def untouched():" in updated
    assert len(updated) == len(original) + len("    'c',\n")


def test_an_anchor_that_is_not_unique_is_rejected(tmp_path: Path) -> None:
    workspace = repository(tmp_path)
    (workspace / "dup.py").write_text("x = 1\nx = 1\n")
    with pytest.raises(Exception, match="matches 2 times"):
        apply_changes(workspace, [{"path": "dup.py", "find": "x = 1\n", "replace": "x = 2\n"}])


def test_an_anchor_that_is_missing_is_rejected(tmp_path: Path) -> None:
    workspace = repository(tmp_path)
    (workspace / "a.py").write_text("real content\n")
    assert not applicable(workspace, [{"path": "a.py", "find": "invented", "replace": "x"}])
    assert applicable(workspace, [{"path": "a.py", "find": "real", "replace": "x"}])


def test_nothing_is_written_when_a_later_change_is_invalid(tmp_path: Path) -> None:
    """A half-applied set would leave the clone corrupted with no way back."""
    workspace = repository(tmp_path)
    (workspace / "first.py").write_text("original\n")
    with pytest.raises(Exception):
        apply_changes(workspace, [
            {"path": "first.py", "content": "overwritten\n"},
            {"path": "second.py", "find": "nope", "replace": "x"},
        ])
    assert (workspace / "first.py").read_text() == "original\n"


def test_an_anchor_with_the_wrong_indentation_still_lands_correctly(tmp_path: Path) -> None:
    """Models mis-copy indentation constantly; rejecting that drove replies to skip the file."""
    workspace = repository(tmp_path)
    (workspace / "screens.py").write_text("OPTIONS = [\n        ('Nord', 'nord'),\n    ]\n")
    apply_changes(workspace, [{
        "path": "screens.py",
        "find": "    ('Nord', 'nord'),\n",                      # 4 spaces; the file has 8
        "replace": "    ('Nord', 'nord'),\n    ('Solarized', 'solarized'),\n",
    }])
    assert (workspace / "screens.py").read_text() == (
        "OPTIONS = [\n        ('Nord', 'nord'),\n        ('Solarized', 'solarized'),\n    ]\n"
    )


def test_a_large_existing_file_cannot_be_rewritten_whole(tmp_path: Path) -> None:
    """Whole-file rewrites of large files are where small models corrupted screens.py."""
    workspace = repository(tmp_path)
    (workspace / "big.py").write_text("x = 1\n" * 1000)
    assert not applicable(workspace, [{"path": "big.py", "content": "x = 2\n"}])
    (workspace / "small.py").write_text("x = 1\n")
    assert applicable(workspace, [{"path": "small.py", "content": "x = 2\n"}])


def test_a_file_the_plan_names_reaches_the_coder_even_when_large(tmp_path: Path) -> None:
    """The gravitype failure: the plan said edit screens.py, but ranking on the issue alone
    dropped it, so the coder invented the text it was asked to anchor an edit to."""
    workspace = repository(tmp_path)
    (workspace / "widgets").mkdir()
    (workspace / "widgets/screens.py").write_text("OPTIONS = ['nord']\n" + "# filler\n" * 400)
    for index in range(30):
        (workspace / f"notes_{index}.md").write_text("theme settings palette colour\n")
    command(["git", "add", "--all"], workspace)
    command(["git", "commit", "-m", "sources"], workspace)

    issue_only = source_context(workspace, "Add a theme selectable in settings", budget=4000)
    with_plan = source_context(workspace, "Add a theme selectable in settings\nEdit widgets/screens.py", budget=4000)
    assert "widgets/screens.py" not in issue_only
    assert with_plan.startswith("--- widgets/screens.py ---")


def test_the_context_never_exceeds_its_budget(tmp_path: Path) -> None:
    """Headers count, so a prompt that caps the context cannot cut a file in half."""
    workspace = repository(tmp_path)
    for index in range(20):
        (workspace / f"f{index}.py").write_text("x = 1\n" * 30)
    command(["git", "add", "--all"], workspace)
    command(["git", "commit", "-m", "sources"], workspace)
    assert len(source_context(workspace, "x", budget=1500)) <= 1500


def test_a_correctly_indented_replacement_is_not_shifted(tmp_path: Path) -> None:
    """The PR #5 regression: anchor 4 spaces short, replacement already right. Shifting it
    anyway re-indented every existing option in the diff."""
    workspace = repository(tmp_path)
    original = (
        "        options=[\n"
        '            ("Dracula", "dracula"),\n'
        '            ("Nord", "nord"),\n'
        "        ],\n"
    )
    (workspace / "screens.py").write_text(original)
    apply_changes(workspace, [{
        "path": "screens.py",
        "find": '        ("Dracula", "dracula"),\n        ("Nord", "nord"),\n    ],\n',      # 4 short
        "replace": (
            '            ("Dracula", "dracula"),\n'                                     # correct
            '            ("Nord", "nord"),\n'
            '            ("Solarized", "solarized_dark"),\n'
            "        ],\n"
        ),
    }])
    updated = (workspace / "screens.py").read_text()
    assert updated == original.replace(
        '            ("Nord", "nord"),\n', '            ("Nord", "nord"),\n            ("Solarized", "solarized_dark"),\n'
    )
    removed = [line for line in original.splitlines() if line not in updated.splitlines()]
    assert removed == [], "no existing line may be re-indented"
