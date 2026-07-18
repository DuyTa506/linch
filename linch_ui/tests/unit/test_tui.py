from __future__ import annotations

from io import StringIO
from pathlib import Path
from types import SimpleNamespace

from linch_studio.spec import load_blueprint_file
from linch_studio.tui import Editor, StudioShell, _color_enabled

GOLDEN = Path(__file__).parents[1] / "golden" / "standard_agent.yaml"


def _shell(workspace: Path, *, editor: Editor | None = None) -> tuple[StudioShell, StringIO]:
    output = StringIO()
    return StudioShell(workspace, stdout=output, editor=editor), output


def _project_from_golden(workspace: Path, name: str = "support_assistant") -> Path:
    project = workspace / name
    project.mkdir(parents=True)
    (project / "linch-studio.yaml").write_text(GOLDEN.read_text(encoding="utf-8"), encoding="utf-8")
    return project


def test_repl_lists_creates_opens_and_reports_status(tmp_path: Path) -> None:
    workspace = tmp_path / "designs"
    shell, output = _shell(workspace)

    shell.onecmd("list")
    shell.onecmd("new nightly-review")
    shell.onecmd("status")
    shell.onecmd("open ../outside")
    shell.onecmd("help preview")

    text = output.getvalue()
    assert "No local projects" in text
    assert "Created and opened nightly_review from agent" in text
    assert "PROJECT SUMMARY" in text
    assert "EXPORT READINESS" in text
    assert "Structure     ● VALID" in text
    assert "Export        ● BLOCKED" in text
    assert "project was not found" in text
    assert "preview [generated-path]" in text


def test_repl_validates_and_previews_generated_content(tmp_path: Path) -> None:
    workspace = tmp_path / "designs"
    _project_from_golden(workspace)
    shell, output = _shell(workspace)

    shell.onecmd("open support_assistant")
    shell.onecmd("validate")
    shell.onecmd("preview")
    shell.onecmd("preview pyproject.toml")

    text = output.getvalue()
    assert "Valid: blueprint is export-ready." in text
    assert "src/support_assistant/agent.py" in text
    assert "[project]" in text


def test_repl_exports_directory_and_zip_without_overwriting(tmp_path: Path) -> None:
    workspace = tmp_path / "designs"
    _project_from_golden(workspace)
    shell, output = _shell(workspace)
    directory = tmp_path / "generated"
    archive = tmp_path / "generated.zip"

    shell.onecmd("open support_assistant")
    shell.onecmd(f"export dir {directory}")
    shell.onecmd(f"export zip {archive}")
    shell.onecmd(f"export dir {directory}")
    shell.onecmd(f"export zip {archive}")

    assert (directory / "pyproject.toml").is_file()
    assert archive.is_file()
    text = output.getvalue()
    assert text.count("Exported ") == 2
    assert "not empty" in text
    assert "refusing to overwrite" in text


def test_repl_editor_restores_structurally_invalid_yaml(tmp_path: Path) -> None:
    workspace = tmp_path / "designs"
    project = _project_from_golden(workspace)
    original = (project / "linch-studio.yaml").read_bytes()

    def break_yaml(path: Path) -> int:
        path.write_text("metadata: [\n", encoding="utf-8")
        return 0

    shell, output = _shell(workspace, editor=break_yaml)
    shell.onecmd("open support_assistant")
    shell.onecmd("edit")

    assert (project / "linch-studio.yaml").read_bytes() == original
    assert "structurally invalid; restored" in output.getvalue()


def test_repl_quit_and_unknown_command_are_non_destructive(tmp_path: Path) -> None:
    shell, output = _shell(tmp_path / "designs")

    assert shell.onecmd("nonsense") is False
    assert shell.onecmd("quit") is True
    assert "unknown command: nonsense" in output.getvalue()


def test_repl_lists_the_shared_templates_without_mimicking_a_canvas(tmp_path: Path) -> None:
    shell, output = _shell(tmp_path / "designs")

    shell.onecmd("templates")

    text = output.getvalue()
    assert "BLUEPRINT TEMPLATES" in text
    assert "goal_verified" in text
    assert "directed_workflow" in text
    assert "routine" in text
    assert "canvas" not in text.casefold()


def test_repl_creates_a_project_from_an_explicit_shared_template(tmp_path: Path) -> None:
    workspace = tmp_path / "designs"
    shell, output = _shell(workspace)

    shell.onecmd("new worktree-watch routine")

    result = load_blueprint_file(workspace / "worktree_watch" / "linch-studio.yaml")
    assert result.blueprint is not None
    assert result.blueprint.spec.routines[0].kind == "agent_tick"
    assert result.blueprint.spec.triggers[0].cron == "0 */2 * * *"
    assert "from routine" in output.getvalue()


def test_migration_notice_is_explicit_about_in_memory_loading(tmp_path: Path) -> None:
    shell, output = _shell(tmp_path / "designs")

    shell._show_migration_notice(  # noqa: SLF001 - narrow rendering contract
        SimpleNamespace(
            migrated_from="studio.linch.dev/v1alpha1",
            migration_warnings=(SimpleNamespace(message="Worker limits were removed."),),
        )
    )

    text = output.getvalue()
    assert "MIGRATION REQUIRED" in text
    assert "in memory" in text
    assert "never overwrites" in text
    assert "Worker limits were removed" in text


def test_rich_color_detection_honors_tty_and_no_color(monkeypatch) -> None:
    class TtyBuffer(StringIO):
        def isatty(self) -> bool:
            return True

    stream = TtyBuffer()
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert _color_enabled(stream) is True

    monkeypatch.setenv("NO_COLOR", "1")
    assert _color_enabled(stream) is False
