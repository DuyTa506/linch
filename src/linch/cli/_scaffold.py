"""Scaffolding logic behind ``linch new`` and ``linch add tool`` (stdlib only)."""

from __future__ import annotations

import keyword
import re
from pathlib import Path

from . import _templates

_NAME_RE = re.compile(r"^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$")
_RESERVED_PROJECT_NAMES = {"linch", "src", "test", "tests"}


class ScaffoldError(Exception):
    """Scaffolding failure carrying its process exit code (2 usage, 1 state)."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _validate_name(name: str, *, kind: str) -> str:
    if not _NAME_RE.match(name):
        raise ScaffoldError(
            f"invalid {kind} name {name!r}: use lowercase letters/digits with single "
            "'-' or '_' separators, starting with a letter",
            exit_code=2,
        )
    snake = name.replace("-", "_")
    if keyword.iskeyword(snake):
        raise ScaffoldError(
            f"invalid {kind} name {name!r}: {snake!r} is a Python keyword", exit_code=2
        )
    if kind == "project" and name in _RESERVED_PROJECT_NAMES:
        raise ScaffoldError(f"invalid project name {name!r}: reserved", exit_code=2)
    return snake


def scaffold_project(name: str, parent: Path) -> list[Path]:
    """Generate a runnable Linch agent project at ``parent / name``.

    Args:
        name: Project name (kebab-case allowed); its snake-case form becomes
            the Python package under ``src/``.
        parent: Directory the project directory is created in.

    Returns:
        The created file paths in creation order.

    Raises:
        ScaffoldError: Invalid name (exit code 2) or non-empty target
            directory (exit code 1).
    """
    package = _validate_name(name, kind="project")
    target = parent / name
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ScaffoldError(f"target {target} already exists and is not empty")
    subs = {"project": name, "package": package}
    files = {
        target / "pyproject.toml": _templates.PYPROJECT,
        target / "README.md": _templates.README,
        target / ".gitignore": _templates.GITIGNORE,
        target / "src" / package / "__init__.py": _templates.PKG_INIT,
        target / "src" / package / "agent.py": _templates.AGENT_MODULE,
        target / "src" / package / "tools" / "__init__.py": _templates.TOOLS_INIT,
        target / "src" / package / "tools" / "greet.py": _templates.GREET_TOOL,
        target / "tests" / "test_agent.py": _templates.TEST_AGENT,
        target / "tests" / "test_greet.py": _templates.TEST_GREET,
    }
    created: list[Path] = []
    for path, template in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(template.substitute(subs), encoding="utf-8")
        created.append(path)
    return created


def add_tool(name: str, start: Path) -> list[Path]:
    """Add a tool stub plus contract test to an existing scaffolded project.

    Args:
        name: Tool name (kebab-case allowed); files use its snake-case form.
        start: Directory to search upward from for the project root.

    Returns:
        The created file paths in creation order.

    Raises:
        ScaffoldError: Invalid name (exit code 2); no project/package found,
            ambiguous package, or existing target files (exit code 1).
    """
    snake = _validate_name(name, kind="tool")
    root = _find_project_root(start)
    package_dir = _find_package_dir(root)
    tool_path = package_dir / "tools" / f"{snake}.py"
    test_path = root / "tests" / f"test_{snake}.py"
    for path in (tool_path, test_path):
        if path.exists():
            raise ScaffoldError(f"refusing to overwrite existing file {path}")
    subs = {"tool": snake, "package": package_dir.name}
    created: list[Path] = []
    tools_init = package_dir / "tools" / "__init__.py"
    if not tools_init.exists():
        tools_init.parent.mkdir(parents=True, exist_ok=True)
        tools_init.write_text(_templates.TOOLS_INIT.substitute(subs), encoding="utf-8")
        created.append(tools_init)
    test_path.parent.mkdir(parents=True, exist_ok=True)
    tool_path.write_text(_templates.TOOL_STUB.substitute(subs), encoding="utf-8")
    created.append(tool_path)
    test_path.write_text(_templates.TOOL_TEST_STUB.substitute(subs), encoding="utf-8")
    created.append(test_path)
    return created


def _find_project_root(start: Path) -> Path:
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise ScaffoldError(
        f"no pyproject.toml found from {start} upward (run inside a project created by 'linch new')"
    )


def _find_package_dir(root: Path) -> Path:
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
    project_name = _project_name_from_pyproject(root / "pyproject.toml")
    if project_name:
        snake = project_name.replace("-", "_")
        for candidate in candidates:
            if candidate.name == snake:
                return candidate
    names = ", ".join(entry.name for entry in candidates)
    raise ScaffoldError(
        f"ambiguous package under {src} (candidates: {names}) and [project].name "
        "does not match any of them"
    )


def _project_name_from_pyproject(path: Path) -> str | None:
    text = path.read_text(encoding="utf-8")
    try:
        import tomllib
    except ImportError:  # Python 3.10 — tomllib is 3.11+
        return _project_name_line_scan(text)
    try:
        data = tomllib.loads(text)
    except Exception:
        return _project_name_line_scan(text)
    name = data.get("project", {}).get("name")
    return name if isinstance(name, str) else None


def _project_name_line_scan(text: str) -> str | None:
    in_project = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_project = stripped == "[project]"
            continue
        if in_project:
            match = re.match(r'\s*name\s*=\s*["\']([^"\']+)["\']', line)
            if match:
                return match.group(1)
    return None
