from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from linch.tools.base import ToolContext


def _session(*handles: Any) -> Any:
    return SimpleNamespace(
        id="parent",
        workers={handle.worker_id: handle for handle in handles},
        agent=SimpleNamespace(_sessions={}),
        pending_child_events=[],
        run_deps=None,
    )


def _handle(worker_id: str, display_name: str) -> Any:
    return SimpleNamespace(
        worker_id=worker_id,
        child_session_id=f"child-{worker_id}",
        display_name=display_name,
        task=None,
        status="running",
    )


def _context() -> ToolContext:
    return ToolContext(
        cwd=".",
        session_id="parent",
        run_id="run",
        session_store=None,
    )


def test_resolve_worker_prefers_exact_id_with_duplicate_display_names() -> None:
    from linch.tools._worker_utils import resolve_worker

    first = _handle("worker-a", "same name")
    second = _handle("worker-b", "same name")

    assert resolve_worker(_session(first, second), "worker-b") is second


def test_resolve_worker_prefers_exact_id_over_display_name_match() -> None:
    from linch.tools._worker_utils import resolve_worker

    display_name_match = _handle("worker-a", "worker-b")
    exact_id_match = _handle("worker-b", "another name")

    assert (
        resolve_worker(_session(display_name_match, exact_id_match), "worker-b") is exact_id_match
    )


async def test_subagent_continue_rejects_ambiguous_display_name(monkeypatch) -> None:
    import linch.subagents.runner as runner
    from linch.subagents.runner import RunSubagentResult
    from linch.tools.subagent_continue import SubagentContinueTool

    first = _handle("worker-a", "same name")
    second = _handle("worker-b", "same name")
    session = _session(first, second)
    called: list[Any] = []

    async def fake_continue(args: Any) -> RunSubagentResult:
        called.append(args)
        return RunSubagentResult(args.handle.child_session_id, "continued", False, False)

    monkeypatch.setattr(runner, "continue_subagent", fake_continue)

    result = await SubagentContinueTool(lambda _session_id: session).execute(
        {"to": "same name", "message": "follow up"}, _context()
    )

    assert result.is_error
    assert "ambiguous" in result.content.lower()
    assert "worker-a" in result.content
    assert "worker-b" in result.content
    assert called == []


async def test_task_stop_rejects_ambiguous_display_name_without_canceling_workers() -> None:
    from linch.tools.subagent_stop import TaskStopTool

    first_task = asyncio.create_task(asyncio.sleep(60))
    second_task = asyncio.create_task(asyncio.sleep(60))
    first = _handle("worker-a", "same name")
    second = _handle("worker-b", "same name")
    first.task = first_task
    second.task = second_task
    session = _session(first, second)

    try:
        result = await TaskStopTool(lambda _session_id: session).execute(
            {"task_id": "same name"}, _context()
        )

        assert result.is_error
        assert "ambiguous" in result.content.lower()
        assert "worker-a" in result.content
        assert "worker-b" in result.content
        assert not first_task.done()
        assert not second_task.done()
        assert first.status == "running"
        assert second.status == "running"
    finally:
        first_task.cancel()
        second_task.cancel()
        await asyncio.gather(first_task, second_task, return_exceptions=True)
