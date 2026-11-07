"""The `linch-rust` console script: scaffold projects on this client, find its docs.

`linch-rust new <name>` writes a runnable agent project that tests offline
on the scripted provider; `linch-rust add tool <name>` adds a host tool and
its test to one; `linch-rust docs path` prints where the docs shipped in the
package are; `linch-rust skill install` copies the `build-agent-with-linch`
skill into a project, so a coding agent finds those docs. The generated code
is plain `linch_rust` usage that the project owns. Standard library only.
"""

from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path

from ._scaffold import (
    SKILL_TARGETS,
    ScaffoldError,
    add_tool,
    docs_root,
    install_skill,
    scaffold_project,
)

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    """Run the `linch-rust` console script.

    It returns instead of raising `SystemExit`, so tests can call it.

    Args:
        argv: The arguments; `sys.argv[1:]` when absent.

    Returns:
        The exit code: 0 on success, 2 for a usage error (an unknown
        command, a missing argument, an invalid name), 1 when the files on
        disk are in the way (a non-empty directory, no project, a file or
        an installed skill that exists, missing docs or skill).
    """
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit:  # --help, and usage errors
        code = exit.code
        if isinstance(code, int):
            return code
        return 0 if code is None else 2
    try:
        return args.handler(args)
    except ScaffoldError as error:
        print(f"linch-rust: error: {error}", file=sys.stderr)
        return error.exit_code
    except OSError as error:
        print(f"linch-rust: error: {error}", file=sys.stderr)
        return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="linch-rust",
        description="Scaffold projects on the Linch Rust client (the code is yours to edit).",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    new = commands.add_parser("new", help="create an agent project")
    new.add_argument("name", help="project name (kebab-case allowed)")
    new.add_argument("--dir", default=".", help="parent directory (default: cwd)")
    new.set_defaults(handler=_new)

    add = commands.add_parser("add", help="add a component to a project")
    kinds = add.add_subparsers(dest="kind", required=True)
    tool = kinds.add_parser("tool", help="add a host tool stub and its test")
    tool.add_argument("name", help="tool name (kebab-case allowed)")
    tool.add_argument("--dir", default=".", help="a directory in the project (default: cwd)")
    tool.set_defaults(handler=_add_tool)

    docs = commands.add_parser("docs", help="find the docs shipped in the package")
    docs_commands = docs.add_subparsers(dest="docs_command", required=True)
    path = docs_commands.add_parser("path", help="print the docs directory")
    path.set_defaults(handler=_docs_path)

    skill = commands.add_parser("skill", help="install the coding-agent skill")
    skill_commands = skill.add_subparsers(dest="skill_command", required=True)
    install = skill_commands.add_parser(
        "install", help="copy build-agent-with-linch into a project"
    )
    install.add_argument(
        "--target",
        action="append",
        choices=sorted(SKILL_TARGETS),
        help="coding agent to install for, repeatable (default: claude and codex)",
    )
    install.add_argument("--dir", default=".", help="project directory (default: cwd)")
    install.add_argument("--force", action="store_true", help="replace an installed copy")
    install.set_defaults(handler=_skill_install)
    return parser


def _new(args: argparse.Namespace) -> int:
    target = Path(args.dir) / args.name
    _print_created(scaffold_project(args.name, Path(args.dir)))
    package = args.name.replace("-", "_")
    print(
        f"\nNext steps:\n"
        f"  cd {shlex.quote(str(target))}\n"
        f"  pip install -e '.[dev]'\n"
        f"  python -m {package}.agent   # runs offline, no API key needed\n"
        f"  pytest"
    )
    return 0


def _add_tool(args: argparse.Namespace) -> int:
    _print_created(add_tool(args.name, Path(args.dir)))
    return 0


def _docs_path(args: argparse.Namespace) -> int:
    print(docs_root())
    return 0


def _skill_install(args: argparse.Namespace) -> int:
    _print_created(install_skill(Path(args.dir), args.target, force=args.force))
    return 0


def _print_created(paths: list[Path]) -> None:
    cwd = Path.cwd()
    for path in paths:
        try:
            shown = path.relative_to(cwd)
        except ValueError:
            shown = path
        print(f"created {shown}")
