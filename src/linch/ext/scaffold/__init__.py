"""The ``linch`` console script: scaffold agent projects you own.

``linch new <name>`` generates a runnable, offline-testable agent project;
``linch add tool <name>`` adds a tool stub plus contract test to an existing
project. Stdlib only — installing linch never pulls CLI dependencies, and the
core import graph is untouched (this package is imported only by the script).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ._scaffold import (
    SKILL_TARGETS,
    ScaffoldError,
    add_tool,
    install_skill,
    scaffold_project,
)

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    """Run the ``linch`` console script.

    Entry point registered as ``[project.scripts] linch``; also callable
    in-process (tests) since it returns instead of raising ``SystemExit``.

    Args:
        argv: Argument list to parse; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code: 0 on success, 2 for usage errors (argparse or
        invalid names), 1 for state/filesystem errors.
    """
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse handles --help and usage errors
        code = exc.code
        if isinstance(code, int):
            return code
        return 0 if code is None else 2
    try:
        return args.handler(args)
    except ScaffoldError as exc:
        print(f"linch: error: {exc}", file=sys.stderr)
        return exc.exit_code
    except OSError as exc:
        print(f"linch: error: {exc}", file=sys.stderr)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="linch",
        description="Scaffold Linch agent projects (generated code is yours to edit).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    new_parser = subparsers.add_parser("new", help="create a new agent project")
    new_parser.add_argument("name", help="project name (kebab-case allowed)")
    new_parser.add_argument("--dir", default=".", help="parent directory (default: cwd)")
    new_parser.set_defaults(handler=_cmd_new)

    add_parser = subparsers.add_parser("add", help="add a component to an existing project")
    add_subparsers = add_parser.add_subparsers(dest="kind", required=True)
    tool_parser = add_subparsers.add_parser("tool", help="add a tool stub + contract test")
    tool_parser.add_argument("name", help="tool name (kebab-case allowed)")
    tool_parser.add_argument("--dir", default=".", help="project directory (default: cwd)")
    tool_parser.set_defaults(handler=_cmd_add_tool)

    docs_parser = subparsers.add_parser("docs", help="locate installed documentation")
    docs_subparsers = docs_parser.add_subparsers(dest="docs_command", required=True)
    path_parser = docs_subparsers.add_parser("path", help="print the docs and examples root")
    path_parser.set_defaults(handler=_cmd_docs_path)

    skill_parser = subparsers.add_parser("skill", help="install the coding-agent skill")
    skill_subparsers = skill_parser.add_subparsers(dest="skill_command", required=True)
    install_parser = skill_subparsers.add_parser(
        "install", help="copy build-agent-with-linch into a project"
    )
    install_parser.add_argument(
        "--target",
        action="append",
        choices=sorted(SKILL_TARGETS),
        help="coding agent to install for, repeatable (default: claude and codex)",
    )
    install_parser.add_argument("--dir", default=".", help="project directory (default: cwd)")
    install_parser.add_argument("--force", action="store_true", help="replace an installed copy")
    install_parser.set_defaults(handler=_cmd_skill_install)

    return parser


def _cmd_new(args: argparse.Namespace) -> int:
    created = scaffold_project(args.name, Path(args.dir))
    _print_created(created)
    package = args.name.replace("-", "_")
    print(
        f"\nNext steps:\n"
        f"  cd {args.name}\n"
        f"  pip install -e '.[dev]'\n"
        f"  python -m {package}.agent   # runs offline, no API key needed\n"
        f"  pytest"
    )
    return 0


def _cmd_add_tool(args: argparse.Namespace) -> int:
    created = add_tool(args.name, Path(args.dir))
    _print_created(created)
    return 0


def _print_created(paths: list[Path]) -> None:
    cwd = Path.cwd()
    for path in paths:
        try:
            display = path.relative_to(cwd)
        except ValueError:
            display = path
        print(f"created {display}")


def _cmd_skill_install(args: argparse.Namespace) -> int:
    for path in install_skill(Path(args.dir), args.target, force=args.force):
        print(path)
    return 0


def _cmd_docs_path(args: argparse.Namespace) -> int:
    source = Path(__file__).resolve()
    candidates = [source.parents[2] / "_docs"]
    candidates.extend(
        parent
        for parent in source.parents
        if source == parent / "src/linch/ext/scaffold/__init__.py"
        and (parent / "pyproject.toml").is_file()
    )
    for root in candidates:
        if (root / "docs/usage/quickstart.md").is_file() and (root / "examples").is_dir():
            print(root)
            return 0
    raise ScaffoldError(
        "docs not found: reinstall linch from a wheel with packaged docs, "
        "or use an editable checkout containing docs/ and examples/"
    )
