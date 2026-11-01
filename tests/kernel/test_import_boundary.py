from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

KERNEL_ROOT = Path(__file__).parents[2] / "src" / "linch" / "kernel"
REPO_ROOT = KERNEL_ROOT.parents[2]


def _violations(source: str, *, relative_path: str = "src/linch/kernel/example.py") -> list[str]:
    tree = ast.parse(source)
    path = Path(relative_path)
    package = ".".join(path.relative_to("src").parent.parts)
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _forbidden_absolute(alias.name):
                    violations.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * node.level + (node.module or "")
            try:
                module = importlib.util.resolve_name(prefix, package) if node.level else prefix
            except ImportError:
                module = prefix
            if _forbidden_absolute(module):
                violations.append(module)
    return violations


def _forbidden_absolute(module: str) -> bool:
    root = module.split(".", 1)[0]
    return (
        root not in sys.stdlib_module_names
        and module != "linch.kernel"
        and not module.startswith("linch.kernel.")
    )


def test_kernel_sources_do_not_import_domain_modules() -> None:
    violations: list[str] = []
    for path in sorted(KERNEL_ROOT.rglob("*.py")):
        relative_path = path.relative_to(REPO_ROOT).as_posix()
        violations.extend(
            f"{path.name}: {module}"
            for module in _violations(path.read_text(), relative_path=relative_path)
        )
    assert violations == []


def test_kernel_errors_keep_public_identity_and_hierarchy() -> None:
    from linch.errors import ConfigError as PublicConfigError
    from linch.errors import LinchError as PublicLinchError
    from linch.kernel.errors import ConfigError, LinchError

    assert PublicLinchError is LinchError
    assert PublicConfigError is ConfigError
    assert issubclass(ConfigError, LinchError)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import linch.errors", ["linch.errors"]),
        ("from linch.lifecycle import LifecyclePoint", ["linch.lifecycle"]),
        ("from ..errors import ConfigError", ["linch.errors"]),
        ("import requests", ["requests"]),
        ("import linch.kernel_helpers", ["linch.kernel_helpers"]),
        ("from linch import lifecycle", ["linch"]),
        ("from .. import lifecycle", ["linch"]),
        ("from linch.kernel.events import EventBus", []),
        ("from .events import EventBus", []),
        ("from ..events import EventBus", ["linch.events"]),
    ],
)
def test_kernel_import_boundary_guard_detects_synthetic_cases(
    source: str, expected: list[str]
) -> None:
    assert _violations(source) == expected


def test_kernel_import_boundary_resolves_nested_relative_imports() -> None:
    assert (
        _violations(
            "from ..events import EventBus",
            relative_path="src/linch/kernel/nested/example.py",
        )
        == []
    )
