"""What `linch-rust new`, `add tool`, `docs path` and `skill install` do (standard library only)."""

from __future__ import annotations

import keyword
import re
import shutil
import tomllib
from collections.abc import Sequence
from pathlib import Path

from .._binary import WORKSPACE
from . import _templates

_NAME = re.compile(r"^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$")
# Package names a project must not take: they would shadow an import.
_RESERVED = {"linch", "linch_rust", "src", "test", "tests"}
_CLIENT = "linch-rust"
_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)")
# A file every docs tree has, to tell a real one from an empty directory.
_DOCS_MARKER = Path("architecture/README.md")
_SKILL = "build-agent-with-linch-rust"
# Where each coding agent looks for a project's skills.
SKILL_TARGETS = {
    "claude": Path(".claude/skills"),
    "codex": Path(".agents/skills"),
    "linch": Path(".linch/skills"),
}
_DEFAULT_SKILL_TARGETS = ("claude", "codex")


class ScaffoldError(Exception):
    """A command that cannot be done, with its exit code (2 usage, 1 files)."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def scaffold_project(name: str, parent: Path) -> list[Path]:
    """Write a runnable agent project on this client at `parent / name`.

    Args:
        name: The project name (kebab-case allowed); its snake-case form is
            the Python package under `src/`.
        parent: The directory the project directory is made in.

    Returns:
        The files written, in order, then the skill directories installed
        for Claude Code and Codex.

    Raises:
        ScaffoldError: The name is invalid (exit code 2), or the target
            exists and is not an empty directory (exit code 1).
    """
    package = _check_name(name, "project")
    target = parent / name
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ScaffoldError(f"target {target} already exists and is not empty")
    names = {"project": name, "package": package}
    files = {
        target / "pyproject.toml": _templates.PYPROJECT,
        target / "README.md": _templates.README,
        target / ".gitignore": _templates.GITIGNORE,
        target / "src" / package / "__init__.py": _templates.PACKAGE_INIT,
        target / "src" / package / "agent.py": _templates.AGENT_MODULE,
        target / "src" / package / "tools" / "__init__.py": _templates.TOOLS_INIT,
        target / "src" / package / "tools" / "greet.py": _templates.GREET_TOOL,
        target / "tests" / "test_agent.py": _templates.TEST_AGENT,
        target / "tests" / "test_greet.py": _templates.TEST_GREET,
    }
    for path, template in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(template.substitute(names), encoding="utf-8")
    return [*files, *install_skill(target)]


def install_skill(
    project: Path, targets: Sequence[str] | None = None, *, force: bool = False
) -> list[Path]:
    """Copy the `build-agent-with-linch-rust` skill shipped with this client into a project.

    Args:
        project: The project directory that receives the skill.
        targets: Keys of `SKILL_TARGETS`, the coding agents to install for;
            Claude Code and Codex when absent.
        force: Whether to replace copies that already exist.

    Returns:
        The installed skill directories, in target order.

    Raises:
        ScaffoldError: The shipped skill is missing, or a copy exists and
            `force` is false; nothing is copied then (exit code 1).
    """
    source = skill_root()
    destinations = [
        project / SKILL_TARGETS[target] / _SKILL
        for target in dict.fromkeys(targets or _DEFAULT_SKILL_TARGETS)
    ]
    existing = [path for path in destinations if path.exists()]
    if existing and not force:
        raise ScaffoldError(f"{existing[0]} already exists; pass --force to replace it")
    for path in destinations:
        if path.exists():
            shutil.rmtree(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, path)
    return destinations


def add_tool(name: str, start: Path) -> list[Path]:
    """Add a host tool stub and its test to the project around `start`.

    The project is the nearest directory at or above `start` whose
    `pyproject.toml` depends on `linch-rust`; its package is the one under
    `src/`, or the one named like the project when there are several.

    Args:
        name: The tool name (kebab-case allowed); the files use its
            snake-case form.
        start: A directory in the project.

    Returns:
        The files written, in order.

    Raises:
        ScaffoldError: The name is invalid (exit code 2); no project or
            package is found, the package is ambiguous, or a file to write
            exists (exit code 1).
    """
    snake = _check_name(name, "tool")
    root = _project_root(start)
    package = _package_dir(root)
    tool_path = package / "tools" / f"{snake}.py"
    test_path = root / "tests" / f"test_{snake}.py"
    for path in (tool_path, test_path):
        if path.exists():
            raise ScaffoldError(f"refusing to overwrite existing file {path}")
    names = {"tool": snake, "package": package.name}
    created: list[Path] = []
    tools_init = package / "tools" / "__init__.py"
    if not tools_init.exists():
        tools_init.parent.mkdir(parents=True, exist_ok=True)
        tools_init.write_text(_templates.TOOLS_INIT.substitute(names), encoding="utf-8")
        created.append(tools_init)
    test_path.parent.mkdir(parents=True, exist_ok=True)
    tool_path.write_text(_templates.TOOL_STUB.substitute(names), encoding="utf-8")
    test_path.write_text(_templates.TOOL_TEST_STUB.substitute(names), encoding="utf-8")
    return [*created, tool_path, test_path]


def docs_root() -> Path:
    """Return the directory of the docs shipped with this client.

    Raises:
        ScaffoldError: Neither the installed package nor the checkout has
            them.
    """
    for root in docs_roots():
        if (root / _DOCS_MARKER).is_file():
            return root
    raise ScaffoldError(
        "docs not found: reinstall linch-rust from a wheel, or use a checkout "
        "that has linch-rust/docs"
    )


def docs_roots() -> list[Path]:
    """Where the docs may be: in the installed package, then in a checkout."""
    return [Path(__file__).resolve().parents[1] / "_docs", WORKSPACE / "docs"]


def skill_root() -> Path:
    """Return the directory of the skill shipped with this client.

    Raises:
        ScaffoldError: Neither the installed package nor the checkout has
            it.
    """
    for root in skill_roots():
        if (root / _SKILL / "SKILL.md").is_file():
            return root / _SKILL
    raise ScaffoldError(
        "skill not found: reinstall linch-rust from a wheel, or use a checkout "
        "that has linch-rust/skills"
    )


def skill_roots() -> list[Path]:
    """Where the skill may be: in the installed package, then in a checkout."""
    return [Path(__file__).resolve().parents[1] / "_skills", WORKSPACE / "skills"]


def _check_name(name: str, kind: str) -> str:
    if not _NAME.match(name):
        raise ScaffoldError(
            f"invalid {kind} name {name!r}: use lowercase letters and digits with single "
            "'-' or '_' separators, starting with a letter",
            exit_code=2,
        )
    snake = name.replace("-", "_")
    if keyword.iskeyword(snake):
        raise ScaffoldError(f"invalid {kind} name {name!r}: {snake!r} is a Python keyword", 2)
    if kind == "project" and snake in _RESERVED:
        raise ScaffoldError(f"invalid project name {name!r}: reserved", 2)
    return snake


def _project_root(start: Path) -> Path:
    current = start.resolve()
    for candidate in (current, *current.parents):
        pyproject = candidate / "pyproject.toml"
        if pyproject.is_file() and _CLIENT in _dependencies(pyproject):
            return candidate
    raise ScaffoldError(
        f"no linch-rust project found from {start} upward (run it inside a project made "
        "by 'linch-rust new'; its pyproject.toml must depend on 'linch-rust')"
    )


def _dependencies(pyproject: Path) -> set[str]:
    """The normalized names in `[project].dependencies`; none if unreadable."""
    project = _project_table(pyproject)
    dependencies = project.get("dependencies")
    if not isinstance(dependencies, list):
        return set()
    names: set[str] = set()
    for requirement in dependencies:  # pyright: ignore[reportUnknownVariableType]
        match = _REQUIREMENT_NAME.match(str(requirement))  # pyright: ignore[reportUnknownArgumentType]
        if match:
            names.add(re.sub(r"[-_.]+", "-", match.group(1)).lower())
    return names


def _project_table(pyproject: Path) -> dict[str, object]:
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError:
        return {}
    project = data.get("project")
    return project if isinstance(project, dict) else {}  # pyright: ignore[reportUnknownVariableType]


def _package_dir(root: Path) -> Path:
    src = root / "src"
    if not src.is_dir():
        raise ScaffoldError(f"no src/ directory under {root}: expected a src-layout project")
    candidates = sorted(
        entry
        for entry in src.iterdir()
        if entry.is_dir() and (entry / "__init__.py").is_file() and entry.name.isidentifier()
    )
    if not candidates:
        raise ScaffoldError(f"no Python package found under {src}")
    if len(candidates) == 1:
        return candidates[0]
    name = _project_table(root / "pyproject.toml").get("name")
    if isinstance(name, str):
        for candidate in candidates:
            if candidate.name == name.replace("-", "_"):
                return candidate
    listed = ", ".join(entry.name for entry in candidates)
    raise ScaffoldError(
        f"ambiguous package under {src} (candidates: {listed}) and [project].name "
        "matches none of them"
    )
