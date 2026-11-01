"""Guard Linch's supported public API and extension boundaries."""

from __future__ import annotations

import ast
import importlib
import subprocess
import sys
import types
from collections import Counter
from collections.abc import Collection
from pathlib import Path

import pytest

import linch

ROOT = Path(__file__).resolve().parents[2]
PROMOTED_EVAL_NAMES = frozenset(
    "CaseResult EvalBenchmarkResult EvalBenchmarkTarget EvalCase EvalResult EvalSuite "
    "EvalTargetResult ScriptedProvider TextTurn ToolUseTurn context_metadata_contains "
    "context_not_trimmed context_selected_tool cost_under load_eval_suite "
    "load_scripted_turns recovery_succeeded run_completed run_eval run_eval_benchmark "
    "schema_valid text_contains tool_called".split()
)
PROMOTED_PERMISSION_NAMES = frozenset("BashRule PathRule Principal ToolRule".split())


def _imported_modules(path: Path, node: ast.Import | ast.ImportFrom, source_root: Path) -> set[str]:
    if isinstance(node, ast.Import):
        return {alias.name for alias in node.names}
    if not isinstance(node, ast.ImportFrom):
        return set()
    package = ".".join(("linch", *path.relative_to(source_root).parent.parts))
    if node.level:
        try:
            module = importlib.util.resolve_name("." * node.level + (node.module or ""), package)
        except ImportError:
            return set()
    else:
        module = node.module
    return set() if module is None else {module, *(f"{module}.{a.name}" for a in node.names)}


def _run_script(script: str, *, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", script], cwd=cwd, capture_output=True, text=True, check=False
    )


def _assert_script_ok(script: str, *, cwd: Path = ROOT) -> None:
    completed = _run_script(script, cwd=cwd)
    assert completed.returncode == 0, completed.stderr


def _assert_setup_public(name: str, value: object) -> None:
    assert name in linch.__all__
    assert hasattr(value, "setup")


def _assert_removed_public(names: Collection[str]) -> None:
    assert not set(names).intersection(linch.__all__)
    assert not any(hasattr(linch, name) for name in names)


def test_plugin_is_public() -> None:
    _assert_setup_public("Plugin", linch.Plugin)


def test_tool_search_plugin_is_public() -> None:
    _assert_setup_public("ToolSearchPlugin", linch.ToolSearchPlugin)


def test_profile_is_public() -> None:
    profile = linch.Profile(id="support", version="1")
    assert "Profile" in linch.__all__
    assert linch.RunOptions(profile=profile).profile is profile


def test_builtin_plugins_are_public_and_catalog_is_removed() -> None:
    assert {"AskUserPlugin", "MailboxPlugin"} <= set(linch.__all__)
    _assert_setup_public("AskUserPlugin", linch.AskUserPlugin)
    _assert_setup_public("MailboxPlugin", linch.MailboxPlugin)
    assert "plugins" not in linch.__all__
    assert not hasattr(linch, "plugins")


def test_resume_rejection_error_is_public() -> None:
    assert "RunNotResumableError" in linch.__all__
    assert issubclass(linch.RunNotResumableError, linch.LinchError)


def test_durable_run_store_names_are_public_and_lazy() -> None:
    expected = set(
        "RunLease RunLeaseStore RunLeaseConflictError LeaseLostError PostgresRunStore".split()
    )
    assert expected <= set(linch.__all__)
    script = f"""
import sys
sys.path.insert(0, {str(ROOT / "src")!r})
import linch
assert 'asyncpg' not in sys.modules
assert 'linch.run_stores.postgres' not in sys.modules
from linch import PostgresRunStore, RunLease, RunLeaseStore, RunLeaseConflictError, LeaseLostError
assert 'asyncpg' not in sys.modules
assert 'linch.run_stores.postgres' in sys.modules
"""
    _assert_script_ok(script)


def test_extension_names_are_explicit() -> None:
    evals = importlib.import_module("linch.ext.evals")
    permissions = importlib.import_module("linch.permissions")

    assert set(evals.__all__) == PROMOTED_EVAL_NAMES
    assert PROMOTED_EVAL_NAMES.isdisjoint(linch.__all__)
    for name in PROMOTED_PERMISSION_NAMES:
        assert getattr(linch, name) is getattr(permissions, name)


def test_extensions_are_explicit_and_removed_from_kernel_surface() -> None:
    from linch.ext.deep_agent import create_deep_agent
    from linch.ext.evals import EvalCase, ScriptedProvider
    from linch.ext.loop_runner import LoopRunner
    from linch.ext.reports import RunReport
    from linch.ext.scaffold import main as scaffold_main
    from linch.ext.workflow import run_workflow

    assert all(callable(v) for v in (create_deep_agent, ScriptedProvider, LoopRunner))
    assert EvalCase.__module__.startswith("linch.ext.evals")
    assert RunReport.__module__ == "linch.ext.reports"
    assert callable(scaffold_main)
    assert callable(run_workflow)
    _assert_removed_public(
        "create_deep_agent EvalCase ScriptedProvider LoopRunner RunReport run_workflow".split()
    )


def test_kernel_has_no_imports_from_extensions() -> None:
    source_root = ROOT / "src" / "linch"
    violations: list[str] = []
    for path in source_root.rglob("*.py"):
        if "ext" in path.relative_to(source_root).parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            for module in _imported_modules(path, node, source_root):
                if module == "linch.ext" or module.startswith("linch.ext."):
                    violations.append(f"{path}:{node.lineno}: import {module}")
    assert violations == []


@pytest.mark.parametrize(
    ("relative_path", "source", "expected"),
    [
        ("agent.py", "from .ext import run_workflow", "linch.ext"),
        ("loop/runner.py", "from .. import ext", "linch.ext"),
        ("__init__.py", "from linch import ext", "linch.ext"),
    ],
)
def test_imported_modules_resolve_extension_boundaries(
    relative_path: str, source: str, expected: str
) -> None:
    source_root = Path("/virtual/src/linch")
    path = source_root / relative_path
    node = next(node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ImportFrom))

    assert expected in _imported_modules(path, node, source_root)


def test_extension_modules_are_explicit() -> None:
    script = f"""
import sys
sys.path.insert(0, {str(ROOT / "src")!r})
import linch
assert "linch.ext.evals" not in sys.modules
assert "linch.permissions" not in sys.modules
from linch.ext.evals import EvalCase
assert "linch.ext.evals" in sys.modules
assert "linch.permissions" not in sys.modules
_ = linch.ToolRule
assert "linch.permissions" in sys.modules
"""
    _assert_script_ok(script)


def test_all_names_resolve() -> None:
    unresolved = [name for name in linch.__all__ if not hasattr(linch, name)]
    assert unresolved == [], f"__all__ names with no attribute: {unresolved}"


def test_reload_skills_is_the_only_catalog_reload_public_name() -> None:
    import inspect

    from linch.agent import Agent

    assert hasattr(Agent, "reload_skills")
    assert inspect.iscoroutinefunction(Agent.reload_skills)
    assert "CatalogChangeBlock" not in linch.__all__
    assert not hasattr(linch, "CatalogChangeBlock")


def test_all_has_no_duplicates() -> None:
    dupes = [name for name, count in Counter(linch.__all__).items() if count > 1]
    assert dupes == [], f"duplicate __all__ entries: {dupes}"


def test_removed_legacy_names_are_not_public() -> None:
    from linch.agent import Agent
    from linch.middleware import MiddlewareContext, ToolCallMiddlewareInput
    from linch.tools.base import ToolContext
    from linch.tools.registry import ToolRegistry

    _assert_removed_public(
        "AgentOptions AgentMiddleware coding_tools defaultTools "
        "default_tools tools_from_defaults".split()
    )
    assert not hasattr(Agent, "openai")
    assert not hasattr(ToolRegistry, "add")
    assert not hasattr(ToolRegistry, "remove")
    for cls, names in (
        (
            ToolContext,
            "sessionId runId sessionStore fileReadTracker idempotencyKey reportProgress".split(),
        ),
        (MiddlewareContext, "sessionId runId turnIndex toolUseId toolName".split()),
        (ToolCallMiddlewareInput, "toolUseId toolName".split()),
    ):
        assert all(not hasattr(cls, name) for name in names)


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


def test_external_consumer_fixture_uses_core_and_extension_api() -> None:
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "public_consumer.py"
    script = f"""
import importlib.util
import sys
sys.path.insert(0, {str(ROOT / "src")!r})
spec = importlib.util.spec_from_file_location("external_consumer", {str(fixture)!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert module.make_agent().tools.list()
assert module.make_contract().fingerprint.startswith("sha256:")
assert module.DeepAgentProfile is not None
"""
    _assert_script_ok(script)


def test_model_call_public_surface_is_limited_to_journal_event() -> None:
    events = importlib.import_module("linch.events")
    assert {"ModelCallEvent", "is_model_call_event"} <= set(linch.__all__)
    assert linch.ModelCallEvent is events.ModelCallEvent
    assert linch.is_model_call_event is events.is_model_call_event
    _assert_removed_public(
        {"ModelCallIntent", "ModelCallReceipt", "AuxiliaryTextResult", "_ModelCallLedger"}
    )


def test_custom_observer_surface_is_removed() -> None:
    _assert_removed_public(
        "BaseObserver LoggingObserver ObserverDispatcher OpenTelemetryObserver ProviderCallInfo "
        "ProviderCallResult RunInfo RunObserver RunResultInfo Span SpanCollector ToolInfo "
        "ToolResultInfo TurnInfo normalize_observers RunTelemetryHook".split()
    )
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("linch.observability")


def test_rag_names_are_public() -> None:
    from linch import RagConnector, RetrievedChunk, register_grounding_check, register_rag_connector

    names = set(
        "RagConnector RetrievedChunk register_grounding_check register_rag_connector".split()
    )
    assert names <= set(linch.__all__)
    assert all(
        callable(value)
        for value in (
            RagConnector,
            RetrievedChunk,
            register_grounding_check,
            register_rag_connector,
        )
    )
