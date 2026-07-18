"""A Rich terminal companion for local Studio projects.

The terminal interface manages blueprints and exports. It deliberately does
not reproduce the graphical workflow canvas, but remains pleasant to use while
the browser Studio is unavailable.
"""

from __future__ import annotations

import cmd
import os
import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TextIO

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from .compiler import CompilerError, ExportError, compile_file, export_directory, export_zip
from .spec import API_VERSION, blueprint_digest, load_blueprint_file
from .templates import DEFAULT_TEMPLATE, get_template, template_summaries

Editor = Callable[[Path], int | None]


class StudioShell(cmd.Cmd):
    """Testable REPL for the local Blueprint -> Validate -> Export flow."""

    intro = ""
    prompt = "linch-studio> "

    def __init__(
        self,
        workspace: Path,
        *,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
        editor: Editor | None = None,
    ) -> None:
        super().__init__(stdin=stdin or sys.stdin, stdout=stdout or sys.stdout)
        self.workspace = workspace
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.current_project: Path | None = None
        self._editor = editor
        self._color = _color_enabled(self.stdout)
        self._console = Console(
            file=self.stdout,
            force_terminal=self._color,
            no_color=not self._color,
            color_system="standard" if self._color else None,
            highlight=False,
            width=100,
        )
        self.intro = ""
        self.prompt = "linch-studio › "

    def preloop(self) -> None:
        self._console.print(self._hero())

    def emptyline(self) -> bool:
        """Do not repeat the previous operation on an empty line."""

        return False

    def default(self, line: str) -> bool:
        self._error(f"unknown command: {line}. Type 'help' for commands.")
        return False

    def do_help(self, arg: str) -> bool | None:
        """Show the command dashboard, or detailed help for one command."""

        if arg.strip():
            return super().do_help(arg)
        self._panel(
            "COMMANDS",
            [
                self._style("PROJECTS", "muted") + "   list · new <name> · open <project-id>",
                self._style("TEMPLATES", "muted") + "  templates · new <name> [template]",
                self._style("AUTHORING", "muted") + "  status · edit · validate",
                self._style("OUTPUT", "muted")
                + "     preview [path] · export dir <path> · export zip <path>",
                self._style("SESSION", "muted") + "    help <command> · quit",
            ],
        )
        return False

    def do_list(self, arg: str) -> bool:
        """list

        List local Blueprint projects in the workspace.
        """

        if self._args(arg, "list") is None:
            return False
        projects = self._projects()
        if not projects:
            self._panel("PROJECTS", ["No local projects yet.", "Create one with: new <name>"])
            return False
        lines = [
            self._style("  PROJECT", "muted") + "                 " + self._style("STATE", "muted")
        ]
        for project in projects:
            result = load_blueprint_file(project / "linch-studio.yaml")
            state = (
                "migrate"
                if result.migrated_from
                else "ready"
                if result.export_ready
                else "draft"
                if result.blueprint
                else "invalid"
            )
            marker = self._style("●", "accent") if project == self.current_project else "○"
            lines.append(f"{marker} {project.name:<24} {self._badge(state)}")
        self._panel("PROJECTS", lines)
        return False

    def do_templates(self, arg: str) -> bool:
        """templates

        List canonical starting points shared with the CLI and browser Studio.
        """

        if self._args(arg, "templates") is None:
            return False
        lines = [
            f"{identifier:<20} {title:<20} {summary}"
            for identifier, title, summary in template_summaries()
        ]
        self._panel("BLUEPRINT TEMPLATES", lines)
        return False

    def do_new(self, arg: str) -> bool:
        """new <name> [template]

        Create and open a local Blueprint draft. The default template is agent.
        """

        values = self._args(arg, "new <name> [template]")
        if values is None or len(values) not in {1, 2}:
            return False
        template_id = values[1] if len(values) == 2 else DEFAULT_TEMPLATE
        try:
            from .cli import create_project

            get_template(template_id)
            if len(values) == 2:
                project = create_project(values[0], self.workspace, template=template_id)
            else:
                project = create_project(values[0], self.workspace)
        except (KeyError, OSError, TypeError, ValueError) as exc:
            self._error(str(exc))
            return False
        self.current_project = project
        self._success(
            f"Created and opened {project.name} from {template_id}. Use 'edit' to configure it."
        )
        return False

    def do_open(self, arg: str) -> bool:
        """open <project-id>

        Select an existing workspace project. Project IDs cannot be paths.
        """

        values = self._args(arg, "open <project-id>")
        if values is None or len(values) != 1:
            return False
        project = self._workspace_project(values[0])
        if project is None or not (project / "linch-studio.yaml").is_file():
            self._error("project was not found in this workspace")
            return False
        self.current_project = project
        self._success(f"Opened {project.name}.")
        self._show_migration_notice(load_blueprint_file(project / "linch-studio.yaml"))
        return False

    def do_status(self, arg: str) -> bool:
        """status

        Show structural validity, export readiness, and canonical digest.
        """

        if self._args(arg, "status") is None:
            return False
        if self.current_project is None:
            self._panel(
                "NO PROJECT OPEN",
                [
                    self._style(f"Workspace     {self.workspace}", "muted"),
                    "Next step      new <name>   or   open <project-id>",
                ],
            )
            return False
        path = self._blueprint_path()
        if path is None:
            return False
        result = load_blueprint_file(path)
        project_name = self.current_project.name if self.current_project else "unknown"
        blueprint = result.blueprint
        lines = [
            f"Project       {self._style(project_name, 'title')}",
            f"Structure     {self._badge('valid' if result.structurally_valid else 'invalid')}",
            f"Export        {self._badge('ready' if result.export_ready else 'blocked')}",
        ]
        if blueprint is not None:
            lines.extend(self._blueprint_summary(blueprint))
            lines.append(self._style(f"Digest        {blueprint_digest(blueprint)}", "muted"))
        lines.append(f"Diagnostics   {len(result.diagnostics)}")
        self._panel("PROJECT SUMMARY", lines)
        self._readiness_panel(result)
        self._show_migration_notice(result)
        return False

    def do_validate(self, arg: str) -> bool:
        """validate

        Validate the selected Blueprint and print actionable diagnostics.
        """

        if self._args(arg, "validate") is None:
            return False
        path = self._blueprint_path()
        if path is None:
            return False
        result = load_blueprint_file(path)
        self._show_migration_notice(result)
        self._write_diagnostics(result.diagnostics)
        if result.export_ready:
            self._success("Valid: blueprint is export-ready.")
        elif not result.diagnostics:
            self._error("blueprint is not export-ready.")
        return False

    def do_preview(self, arg: str) -> bool:
        """preview [generated-path]

        List generated files, or print one generated file without writing it.
        """

        values = self._args(arg, "preview [generated-path]")
        if values is None or len(values) > 1:
            return False
        path = self._blueprint_path()
        if path is None:
            return False
        try:
            project = compile_file(path)
            if not values:
                files = project.preview()
                lines = [
                    self._style(item["path"], "title")
                    + "  "
                    + self._style("[" + item["capabilityId"] + "]", "muted")
                    for item in files
                ]
                self._panel(f"GENERATED FILES · {len(files)}", lines)
                return False
            item = project.file(values[0])
        except CompilerError as exc:
            self._write_diagnostics(exc.diagnostics)
            return False
        except (KeyError, ValueError):
            self._error("generated file was not found")
            return False
        self._write(item.content)
        if not item.content.endswith("\n"):
            self._write("\n")
        return False

    def do_export(self, arg: str) -> bool:
        """export dir <new-or-empty-directory>
        export zip <new-zip-path>

        Export the selected blueprint. Existing generated directories and ZIPs
        are always refused.
        """

        values = self._args(arg, "export (dir|zip) <destination>")
        if values is None or len(values) != 2 or values[0] not in {"dir", "zip"}:
            return False
        path = self._blueprint_path()
        if path is None:
            return False
        destination = Path(values[1])
        try:
            project = compile_file(path)
            written = (
                export_directory(project, destination)
                if values[0] == "dir"
                else export_zip(project, destination)
            )
        except CompilerError as exc:
            self._write_diagnostics(exc.diagnostics)
            return False
        except (ExportError, OSError, ValueError) as exc:
            self._error(str(exc))
            return False
        self._success(f"Exported {written}")
        return False

    def do_edit(self, arg: str) -> bool:
        """edit

        Open the selected YAML file using the injected editor or $EDITOR. The
        original file is restored if the editor leaves structurally invalid YAML.
        """

        if self._args(arg, "edit") is None:
            return False
        path = self._blueprint_path()
        if path is None:
            return False
        original = path.read_bytes()
        try:
            code = self._run_editor(path)
        except (OSError, ValueError) as exc:
            self._error(f"editor failed: {exc}")
            return False

        result = load_blueprint_file(path)
        if not result.structurally_valid:
            path.write_bytes(original)
            self._error("editor changes were structurally invalid; restored the original draft")
            self._write_diagnostics(result.diagnostics)
            return False
        if code not in (None, 0):
            self._error(f"editor exited with status {code}; valid changes were retained")
        if result.export_ready:
            self._success("Draft saved and export-ready.")
        else:
            self._warning("Draft saved with semantic diagnostics; export remains blocked.")
            self._write_diagnostics(result.diagnostics)
        return False

    def do_quit(self, arg: str) -> bool:
        """quit

        Leave the terminal companion.
        """

        if self._args(arg, "quit") is None:
            return False
        return True

    def do_exit(self, arg: str) -> bool:
        """exit

        Alias for quit.
        """

        return self.do_quit(arg)

    def do_EOF(self, arg: str) -> bool:  # noqa: N802 - cmd dispatch convention
        self._write("\n")
        return True

    def _args(self, raw: str, usage: str) -> list[str] | None:
        try:
            return shlex.split(raw)
        except ValueError as exc:
            self._error(f"invalid arguments: {exc}. Usage: {usage}")
            return None

    def _projects(self) -> list[Path]:
        return sorted(
            (
                item
                for item in self.workspace.iterdir()
                if item.is_dir()
                and not item.is_symlink()
                and (item / "linch-studio.yaml").is_file()
            ),
            key=lambda item: item.name,
        )

    def _workspace_project(self, name: str) -> Path | None:
        if not name or Path(name).name != name or name in {".", ".."}:
            return None
        candidate = self.workspace / name
        if candidate.is_symlink() or not candidate.is_dir():
            return None
        return candidate

    def _blueprint_path(self) -> Path | None:
        if self.current_project is None:
            self._error("no project is open; use list, new, or open first")
            return None
        path = self.current_project / "linch-studio.yaml"
        if not path.is_file():
            self._error("the selected project has no linch-studio.yaml")
            return None
        return path

    def _run_editor(self, path: Path) -> int | None:
        if self._editor is not None:
            return self._editor(path)
        editor = os.environ.get("EDITOR", "").strip()
        if not editor:
            raise ValueError("no editor is configured; set $EDITOR or provide an editor callable")
        command = shlex.split(editor)
        if not command:
            raise ValueError("$EDITOR did not contain a command")
        return subprocess.run([*command, str(path)], check=False).returncode

    def _write_diagnostics(self, diagnostics: Sequence[object]) -> None:
        for item in diagnostics:
            payload = item.model_dump(mode="json")  # type: ignore[attr-defined]
            severity = str(payload["severity"])
            style = "error" if severity == "error" else "warning"
            self._write(
                self._style(f"{severity.upper()} {payload['code']} {payload['path']}", style)
                + f"\n  {payload['message']}\n  Remediation: {payload['remediation']}\n"
            )

    def _write(self, message: str) -> None:
        self._console.print(Text(message), end="")

    def _error(self, message: str) -> None:
        self._console.print(f"Error: {message}", style="bold red", markup=False)

    def _success(self, message: str) -> None:
        self._console.print(f"✓ {message}", style="bold green", markup=False)

    def _warning(self, message: str) -> None:
        self._console.print(f"! {message}", style="bold yellow", markup=False)

    def _badge(self, state: str) -> str:
        return f"● {state.upper()}"

    def _panel(self, title: str, lines: Sequence[str]) -> None:
        body = Text("\n".join(lines) if lines else "—")
        self._console.print(Panel(body, title=title, border_style="cyan", expand=False))

    def _hero(self) -> Panel:
        return Panel(
            Text(
                "LINCH STUDIO · LOCAL BLUEPRINT CONSOLE\n"
                "Template → validate → preview → export\n"
                "Type 'help' to see commands. Use 'new <name> [template]' to begin."
            ),
            border_style="cyan",
            expand=False,
        )

    def _style(self, message: str, style: str) -> str:
        del style
        return message

    def _blueprint_summary(self, blueprint: object) -> list[str]:
        spec = getattr(blueprint, "spec", None)
        runtime = getattr(spec, "runtime", None)
        agent = getattr(runtime, "agent", None)
        legacy_agent = getattr(spec, "primary_agent", None)
        preset = getattr(agent, "preset", None) or getattr(legacy_agent, "mode", "unknown")
        workflows = getattr(spec, "workflows", ())
        routines = getattr(spec, "routines", getattr(spec, "loops", ()))
        return [
            f"Blueprint     {getattr(blueprint, 'api_version', API_VERSION)}",
            f"Agent loop    {preset}",
            f"Components    {len(getattr(spec, 'tools', ()))} tools · "
            f"{len(getattr(spec, 'subagents', ()))} subagents",
            f"Orchestration {len(workflows)} workflows · {len(routines)} routines",
        ]

    def _readiness_panel(self, result: object) -> None:
        diagnostics = tuple(getattr(result, "diagnostics", ()))
        errors = sum(getattr(item, "severity", None) == "error" for item in diagnostics)
        warnings = sum(getattr(item, "severity", None) == "warning" for item in diagnostics)
        ready = bool(getattr(result, "export_ready", False))
        lines = [
            f"State         {self._badge('ready' if ready else 'blocked')}",
            f"Findings      {errors} blocking · {warnings} warning",
            (
                "Next step     preview or export"
                if ready
                else "Next step     resolve blocking diagnostics, then validate again"
            ),
        ]
        self._panel("EXPORT READINESS", lines)

    def _show_migration_notice(self, result: object) -> None:
        migrated_from = getattr(result, "migrated_from", None)
        if not migrated_from:
            return
        warnings = getattr(result, "migration_warnings", ())
        lines = [
            f"Loaded        {migrated_from} → {API_VERSION} in memory",
            "On disk       unchanged; Studio never overwrites a legacy blueprint implicitly",
            "Next step     review warnings, then use the explicit migrate/save action",
        ]
        for warning in warnings:
            lines.append(f"Warning       {getattr(warning, 'message', warning)}")
        self._panel("MIGRATION REQUIRED", lines)


def run_tui(workspace: Path) -> int:
    """Start the interactive companion and return a CLI-compatible status."""

    StudioShell(workspace).cmdloop()
    return 0


def _color_enabled(stream: object) -> bool:
    """Let Rich color a capable TTY while honoring standard opt-out signals."""

    return bool(
        getattr(stream, "isatty", lambda: False)()
        and os.environ.get("NO_COLOR") is None
        and os.environ.get("TERM", "").lower() != "dumb"
    )


__all__ = ["Editor", "StudioShell", "run_tui"]
