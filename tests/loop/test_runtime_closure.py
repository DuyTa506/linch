from __future__ import annotations

from typing import Any


async def test_budget_terminal_closes_delegated_tail(monkeypatch: Any) -> None:
    from linch.events import ResultEvent
    from linch.loop import runtime as runtime_module
    from linch.loop.runtime import RunRuntime
    from linch.types import Usage

    closed = False

    async def tail(*_args: Any, **_kwargs: Any):
        nonlocal closed
        try:
            yield ResultEvent("error", "error", Usage(), 0)
            await __import__("asyncio").sleep(3600)
        finally:
            closed = True

    monkeypatch.setattr(runtime_module, "_budget_exhausted_tail", tail)
    runtime = object.__new__(RunRuntime)
    runtime.session = object()
    runtime.run_id = "run"
    runtime.budget = object()
    runtime.total = Usage()
    runtime.started = 0.0
    runtime.running_cost = None
    runtime.ledger = type("Ledger", (), {"usage_complete": True})()
    runtime.sync_checkpoint_accounting = lambda: None
    runtime.build_run_result = lambda *args, **kwargs: None

    iterator = runtime.emit_budget_exhausted().__aiter__()
    await anext(iterator)
    await iterator.aclose()

    assert closed


async def test_abort_terminal_closes_exception_tail(monkeypatch: Any) -> None:
    from linch.events import ResultEvent
    from linch.loop import runtime as runtime_module
    from linch.loop.runtime import RunRuntime
    from linch.types import Usage

    closed = False

    async def tail(*_args: Any, **_kwargs: Any):
        nonlocal closed
        try:
            yield ResultEvent("error", "error", Usage(), 0)
            await __import__("asyncio").sleep(3600)
        finally:
            closed = True

    async def close_active_spans(**_kwargs: Any) -> None:
        return None

    async def attempt(label: str, _operation: Any, _original: Exception) -> Exception | None:
        return RuntimeError("event write failed") if label == "event append" else None

    async def exception_terminal(_exc: Exception):
        nonlocal closed
        try:
            yield ResultEvent("error", "error", Usage(), 0)
            await __import__("asyncio").sleep(3600)
        finally:
            closed = True

    monkeypatch.setattr(runtime_module, "cancel_background_workers", lambda _session: _done())
    runtime = object.__new__(RunRuntime)
    runtime.session = type("Session", (), {"workers": {}, "background_tasks": []})()
    runtime.agent = type("Agent", (), {"run_store": None})()
    runtime.run_id = "run"
    runtime.run_record = None
    runtime.started = 0.0
    runtime.total = Usage()
    runtime.running_cost = None
    runtime.ledger = type("Ledger", (), {"usage_complete": True})()
    runtime.build_run_result = lambda *args, **kwargs: None
    runtime.sync_checkpoint_accounting = lambda: None
    runtime._attempt = attempt
    runtime.emit_exception_terminal = exception_terminal

    iterator = runtime.emit_abort_terminal(close_active_spans=close_active_spans).__aiter__()
    await anext(iterator)
    await iterator.aclose()

    assert closed


async def _done() -> None:
    return None
