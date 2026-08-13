"""Public-surface contract guard (ROADMAP Phase 5.1).

`linch.__all__` is the supported, semver-governed public API. This test pins the
contract so an accidental rename/removal or an undeclared public leak is caught
in CI rather than in an embedder's pinned import:

- every name in `__all__` resolves on the package,
- `__all__` has no duplicate entries,
- every public (non-underscore) *attribute* of the package — submodules aside —
  is declared in `__all__`, so nothing leaks into the surface undeclared.

Submodule names (`linch.agent`, `linch.tools`, …) are import artifacts, not part
of the contract: embedders import from the top-level `linch` namespace only.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
import types
from collections import Counter
from pathlib import Path

import linch

PROMOTED_EVAL_NAMES = frozenset(
    {
        "CaseResult",
        "EvalBenchmarkResult",
        "EvalBenchmarkTarget",
        "EvalCase",
        "EvalResult",
        "EvalSuite",
        "EvalTargetResult",
        "ScriptedProvider",
        "TextTurn",
        "ToolUseTurn",
        "context_metadata_contains",
        "context_not_trimmed",
        "context_selected_tool",
        "cost_under",
        "load_eval_suite",
        "load_scripted_turns",
        "memory_recalled",
        "recovery_succeeded",
        "run_completed",
        "run_eval",
        "run_eval_benchmark",
        "schema_valid",
        "text_contains",
        "tool_called",
    }
)
PROMOTED_PERMISSION_NAMES = frozenset({"BashRule", "PathRule", "ToolRule"})


def test_studio_prerequisite_names_are_promoted() -> None:
    evals = importlib.import_module("linch.evals")
    permissions = importlib.import_module("linch.permissions")

    assert set(evals.__all__) == PROMOTED_EVAL_NAMES
    expected = PROMOTED_EVAL_NAMES | PROMOTED_PERMISSION_NAMES
    assert expected <= set(linch.__all__)
    for name in PROMOTED_EVAL_NAMES:
        assert getattr(linch, name) is getattr(evals, name)
    for name in PROMOTED_PERMISSION_NAMES:
        assert getattr(linch, name) is getattr(permissions, name)


def test_studio_prerequisite_modules_remain_lazy() -> None:
    root = Path(__file__).resolve().parents[1]
    script = f"""
import sys
sys.path.insert(0, {str(root / "src")!r})
import linch
assert "linch.evals" not in sys.modules
assert "linch.permissions" not in sys.modules
_ = linch.EvalCase
assert "linch.evals" in sys.modules
assert "linch.permissions" not in sys.modules
_ = linch.ToolRule
assert "linch.permissions" in sys.modules
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_all_names_resolve() -> None:
    unresolved = [name for name in linch.__all__ if not hasattr(linch, name)]
    assert unresolved == [], f"__all__ names with no attribute: {unresolved}"


def test_all_has_no_duplicates() -> None:
    dupes = [name for name, count in Counter(linch.__all__).items() if count > 1]
    assert dupes == [], f"duplicate __all__ entries: {dupes}"


def test_no_undeclared_public_attributes() -> None:
    declared = set(linch.__all__)
    leaked = [
        name
        for name in dir(linch)
        if not name.startswith("_")
        and name not in declared
        and not isinstance(getattr(linch, name), types.ModuleType)
    ]
    assert leaked == [], f"public attributes missing from __all__: {leaked}"


def test_external_consumer_fixture_uses_only_top_level_api() -> None:
    """A consumer module must not need private Linch submodule paths."""

    fixture = Path(__file__).parent / "fixtures" / "public_consumer.py"
    script = f"""
import importlib.util
import sys
sys.path.insert(0, {str(Path(__file__).resolve().parents[1] / "src")!r})
spec = importlib.util.spec_from_file_location("external_consumer", {str(fixture)!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert module.make_agent().tools.list()
assert module.make_contract().fingerprint.startswith("sha256:")
assert module.DeepAgentProfile is not None
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
