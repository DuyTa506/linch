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
