"""Command-line contract for local Studio projects and deterministic export."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from ._version import __version__
from .compiler import CompilerError, ExportError, compile_file, export_directory, export_zip
from .spec import (
    Blueprint,
    default_blueprint,
    dump_blueprint,
    load_blueprint_file,
)
from .templates import DEFAULT_TEMPLATE, TEMPLATE_IDS

_PROJECT_NAME_RE = re.compile(r"^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$")


class CLIError(RuntimeError):
    def __init__(self, message: str, *, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="linch-studio",
        description="Local-first Linch blueprint editor and skeleton exporter.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subcommands = parser.add_subparsers(dest="command", required=True)

    serve = subcommands.add_parser("serve", help="serve the local Studio UI on 127.0.0.1")
    serve.add_argument("--workspace", type=Path, default=Path("designs"))
    serve.add_argument("--port", type=int, default=8765)

    tui = subcommands.add_parser("tui", help="open the interactive local terminal companion")
    tui.add_argument("--workspace", type=Path, default=Path("designs"))

    new = subcommands.add_parser("new", help="create a new blueprint draft")
    new.add_argument("name")
    new.add_argument("--workspace", type=Path, default=Path("designs"))
    new.add_argument("--template", choices=TEMPLATE_IDS, default=DEFAULT_TEMPLATE)

    validate = subcommands.add_parser("validate", help="validate one blueprint")
    validate.add_argument("blueprint", type=Path)
    validate.add_argument("--json", action="store_true", dest="as_json")

    preview = subcommands.add_parser("preview", help="compile and list generated files")
    preview.add_argument("blueprint", type=Path)
    preview.add_argument("--json", action="store_true", dest="as_json")
    preview.add_argument("--content", action="store_true")

    export = subcommands.add_parser("export", help="export to an empty directory or new ZIP")
    export.add_argument("blueprint", type=Path)
    destination = export.add_mutually_exclusive_group(required=True)
    destination.add_argument("--out", type=Path)
    destination.add_argument("--zip", type=Path, dest="zip_path")

    migrate = subcommands.add_parser(
        "migrate",
        help="write a migrated v1alpha2 blueprint to a new file",
    )
    migrate.add_argument("blueprint", type=Path)
    migrate.add_argument("--out", type=Path, required=True)
    migrate.add_argument(
        "--accept-dropped-worker-limits",
        action="store_true",
        help="acknowledge removal of legacy unenforced subagent limits",
    )

    schema = subcommands.add_parser("schema", help="write the v1alpha2 JSON Schema")
    schema.add_argument("--out", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        if args.command == "serve":
            return _serve(args.workspace, args.port)
        if args.command == "tui":
            return _tui(args.workspace)
        if args.command == "new":
            return _new(args.name, args.workspace, template=args.template)
        if args.command == "validate":
            return _validate(args.blueprint, as_json=args.as_json)
        if args.command == "preview":
            return _preview(args.blueprint, as_json=args.as_json, include_content=args.content)
        if args.command == "export":
            return _export(args.blueprint, out=args.out, zip_path=args.zip_path)
        if args.command == "migrate":
            return _migrate(
                args.blueprint,
                out=args.out,
                accept_dropped_worker_limits=args.accept_dropped_worker_limits,
            )
        if args.command == "schema":
            return _schema(args.out)
        raise CLIError("unknown command", exit_code=2)
    except CLIError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    except CompilerError as exc:
        _print_diagnostics(exc.diagnostics, as_json=False)
        return 1
    except ExportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError, ValidationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _serve(workspace: Path, port: int) -> int:
    if not 1 <= port <= 65_535:
        raise CLIError("port must be between 1 and 65535", exit_code=2)
    try:
        import uvicorn
    except ModuleNotFoundError as exc:
        raise CLIError("serve requires uvicorn; install linch-studio") from exc
    from .authoring import AuthoringConfigurationError
    from .server import authoring_service_from_env, create_app, support_service_from_env

    workspace.mkdir(parents=True, exist_ok=True)
    try:
        authoring_service = authoring_service_from_env()
        support_service = support_service_from_env()
    except AuthoringConfigurationError as exc:
        raise CLIError("invalid AI authoring configuration") from exc
    app = create_app(
        workspace=workspace,
        authoring_service=authoring_service,
        support_service=support_service,
    )
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
    return 0


def _tui(workspace: Path) -> int:
    from .tui import run_tui

    return run_tui(workspace)


def _new(name: str, workspace: Path, *, template: str = DEFAULT_TEMPLATE) -> int:
    try:
        target = create_project(name, workspace, template=template)
    except ValueError as exc:
        message = str(exc)
        usage_error = message.startswith("project name")
        raise CLIError(message, exit_code=2 if usage_error else 1) from exc
    print(target / "linch-studio.yaml")
    print("Draft saved. Choose a provider and model before export.")
    return 0


def create_project(
    name: str,
    workspace: Path,
    *,
    template: str = DEFAULT_TEMPLATE,
) -> Path:
    """Create one empty local project without replacing an existing draft."""

    if not _PROJECT_NAME_RE.fullmatch(name):
        raise ValueError(
            "project name must start with a lowercase letter and contain only "
            "lowercase letters, digits, '-' or '_' separators"
        )
    project_id = name.replace("-", "_")
    try:
        blueprint = default_blueprint(project_id, template=template)
    except ValidationError as exc:
        raise ValueError("project name cannot be emitted safely") from exc
    workspace.mkdir(parents=True, exist_ok=True)
    target = workspace / project_id
    if target.is_symlink():
        raise ValueError(f"refusing symlink project target {target}")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ValueError(f"project target already exists and is not empty: {target}")

    stage = Path(tempfile.mkdtemp(prefix=f".{project_id}.linch-studio-", dir=workspace))
    try:
        (stage / ".linch-studio").mkdir()
        _write_new(stage / "linch-studio.yaml", dump_blueprint(blueprint))
        _write_new(
            stage / ".linch-studio" / "layout.json",
            json.dumps(
                {"nodes": [], "viewport": {"x": 0.0, "y": 0.0, "zoom": 1.0}},
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        if target.exists():
            target.rmdir()
        os.rename(stage, target)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return target


def _validate(path: Path, *, as_json: bool) -> int:
    result = load_blueprint_file(path)
    _print_diagnostics(result.diagnostics, as_json=as_json)
    if result.export_ready:
        if not as_json:
            print("valid: blueprint is export-ready")
        return 0
    if not result.diagnostics and not as_json:
        print("invalid: blueprint is not export-ready")
    return 1


def _preview(path: Path, *, as_json: bool, include_content: bool) -> int:
    project = compile_file(path)
    preview = project.preview()
    if not include_content:
        for item in preview:
            item.pop("content", None)
    if as_json:
        print(json.dumps(preview, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        for item in preview:
            print(f"{item['sha256']}  {item['path']}  [{item['capabilityId']}]")
            if include_content:
                print(item["content"], end="")
    return 0


def _export(path: Path, *, out: Path | None, zip_path: Path | None) -> int:
    project = compile_file(path)
    destination = (
        export_directory(project, out)
        if out is not None
        else export_zip(project, _required_path(zip_path))
    )
    print(destination)
    return 0


def _migrate(
    path: Path,
    *,
    out: Path,
    accept_dropped_worker_limits: bool,
) -> int:
    """Materialize an in-memory migration without replacing either input or output."""

    result = load_blueprint_file(path)
    if result.blueprint is None:
        _print_diagnostics(result.diagnostics, as_json=False)
        return 1
    if result.migrated_from is None:
        raise CLIError("blueprint already uses the current apiVersion", exit_code=2)
    dropped_limits = any(
        warning.code == "migration.worker_limits_dropped" for warning in result.migration_warnings
    )
    if dropped_limits and not accept_dropped_worker_limits:
        _print_diagnostics(result.diagnostics, as_json=False)
        raise CLIError(
            "migration removes legacy unenforced worker limits; review the warning and rerun "
            "with --accept-dropped-worker-limits",
            exit_code=2,
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        _write_new(out, dump_blueprint(result.blueprint))
    except FileExistsError as exc:
        raise CLIError(f"refusing to overwrite existing migration output: {out}") from exc
    print(out)
    return 0


def _schema(path: Path) -> int:
    document = Blueprint.model_json_schema(by_alias=True, mode="validation")
    content = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(temp_fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except Exception:
        Path(temp_name).unlink(missing_ok=True)
        raise
    print(path)
    return 0


def _print_diagnostics(diagnostics: Sequence[object], *, as_json: bool) -> None:
    payload = [item.model_dump(mode="json") for item in diagnostics]  # type: ignore[attr-defined]
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    if not payload:
        return
    for item in payload:
        print(
            f"{str(item['severity']).upper()} {item['code']} {item['path']}: "
            f"{item['message']} Remediation: {item['remediation']}",
            file=sys.stderr if item["severity"] == "error" else sys.stdout,
        )


def _write_new(path: Path, content: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)


def _required_path(path: Path | None) -> Path:
    if path is None:
        raise CLIError("ZIP destination is required", exit_code=2)
    return path


__all__ = ["build_parser", "create_project", "main"]
