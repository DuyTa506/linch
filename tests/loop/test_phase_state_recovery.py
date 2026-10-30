"""Regression coverage for mutable state crossing extracted loop phases."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest


class _RepeatToolProvider:
    id = "phase-repeat"

    def __init__(self) -> None:
        self.calls = 0

    def context_window(self, model: str) -> int:
        return 100_000

    async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
        from linch.types import Usage

        self.calls += 1
        yield {"type": "message_start", "model": req.model}
        yield {"type": "tool_use_start", "id": f"call-{self.calls}", "name": "Repeat"}
        yield {
            "type": "tool_use_input_delta",
            "id": f"call-{self.calls}",
            "json_delta": json.dumps({"value": "same"}),
        }
        yield {"type": "tool_use_end", "id": f"call-{self.calls}"}
        yield {
            "type": "message_end",
            "stop_reason": "tool_use",
            "usage": Usage(),
        }


class _FinalOnlyProvider:
    id = "phase-repeat"

    def __init__(self) -> None:
        self.requests: list[Any] = []

    def context_window(self, model: str) -> int:
        return 100_000

    async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
        from linch.types import Usage

        self.requests.append(req)
        yield {"type": "message_start", "model": req.model}
        yield {"type": "text_delta", "text": "final"}
        yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}


class _AbortProvider:
    id = "close-abort"

    def context_window(self, model: str) -> int:
        return 100_000

    async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
        from linch.errors import AbortError

        raise AbortError("user cancelled")
        yield  # pragma: no cover


class _ErrorProvider:
    id = "close-error"

    def context_window(self, model: str) -> int:
        return 100_000

    async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
        from linch.errors import ProviderError

        raise ProviderError("provider exploded")
        yield  # pragma: no cover


class _BudgetProvider:
    id = "close-budget"

    def context_window(self, model: str) -> int:
        return 100_000

    async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
        raise AssertionError("budget terminal should precede provider dispatch")
        yield  # pragma: no cover


class _RepeatTool:
    name = "Repeat"
    description = "Repeat a harmless operation."
    input_schema = {"type": "object", "properties": {"value": {"type": "string"}}}
    scope = "read"
    parallel = False
    tags: tuple[str, ...] = ()

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        return raw

    def summarize(self, input: dict[str, Any]) -> str:
        return "repeat"

    def resources(self, input: dict[str, Any]) -> list[Any]:
        return []

    async def execute(self, input: dict[str, Any], ctx: Any) -> Any:
        from linch.tools import ToolResult

        return ToolResult(content="ok")


def _agent(
    provider: Any,
    *,
    session_store: Any,
    run_store: Any,
    hooks: list[Any] | None = None,
) -> Any:
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.loop_guard import LoopGuard
    from linch.tools.registry import empty_tools

    return Agent(
        model="test-model",
        provider=provider,
        tools=empty_tools(_RepeatTool()),
        permissions={"mode": "skip-dangerous"},
        session_store=session_store,
        run_store=run_store,
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        loop_guard=LoopGuard(max_identical_tool_calls=2, force_final_answer=True),
        hooks=hooks,
    )


async def _leave_at_force_final_checkpoint(session_store: Any, run_store: Any) -> str:
    provider = _RepeatToolProvider()
    agent = _agent(provider, session_store=session_store, run_store=run_store)
    session = await agent.session(id="phase-state")
    iterator = session.run("repeat until stopped").__aiter__()
    run_id: str | None = None
    try:
        async for event in iterator:
            if event.type == "system":
                run_id = event.run_id
            if event.type == "user" and getattr(event, "subtype", None) == "notification":
                break
    finally:
        await iterator.aclose()
    assert run_id is not None
    record = await run_store.load_run(run_id)
    assert record is not None and record.checkpoint is not None
    assert record.checkpoint.phase == "turn_complete"
    assert record.checkpoint.force_final_pending is True
    return run_id


@pytest.mark.asyncio
async def test_force_final_checkpoint_survives_phase_state_transfer() -> None:
    from linch.run_store import InMemoryRunStore
    from linch.sessions import InMemorySessionStore

    session_store = InMemorySessionStore()
    run_store = InMemoryRunStore()
    run_id = await _leave_at_force_final_checkpoint(session_store, run_store)

    provider = _FinalOnlyProvider()
    restarted = _agent(provider, session_store=session_store, run_store=run_store)
    session = await restarted.session(id="phase-state")
    events = [event async for event in session.resume(run_id)]

    assert provider.requests
    assert provider.requests[0].tools == []
    assert provider.requests[0].tool_choice is None
    assert events[-1].type == "result"
    assert events[-1].subtype == "success"


@pytest.mark.asyncio
async def test_provider_checkpoint_clears_force_final_state_before_persist() -> None:
    from linch.run_store import InMemoryRunStore
    from linch.sessions import InMemorySessionStore

    session_store = InMemorySessionStore()
    run_store = InMemoryRunStore()
    run_id = await _leave_at_force_final_checkpoint(session_store, run_store)

    provider = _FinalOnlyProvider()
    restarted = _agent(provider, session_store=session_store, run_store=run_store)
    session = await restarted.session(id="phase-state")
    iterator = session.resume(run_id).__aiter__()
    try:
        async for event in iterator:
            if event.type == "assistant":
                break
    finally:
        await iterator.aclose()

    record = await run_store.load_run(run_id)
    assert record is not None and record.checkpoint is not None
    assert record.checkpoint.phase == "assistant_appended"
    assert record.checkpoint.force_final_pending is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "terminal_type", "subtype", "stop_reason", "error_name"),
    [
        ("abort", "result", "aborted", "error", None),
        ("provider_error", "error", "error", "error", "ProviderError"),
        ("budget", "budget", "error", "error", None),
    ],
)
async def test_terminal_close_preserves_agent_stop_result(
    case: str,
    terminal_type: str,
    subtype: str,
    stop_reason: str,
    error_name: str | None,
) -> None:
    """Closing at the first terminal-tail yield must retain its run result."""
    from linch import RunBudget, RunOptions
    from linch.loop import _run_loop_impl
    from linch.run_store import InMemoryRunStore
    from linch.sessions import InMemorySessionStore

    class _AgentStopHook:
        def __init__(self) -> None:
            self.results: list[Any] = []

        def on_agent_stop(self, ctx: Any) -> None:
            self.results.append(ctx.result)

    providers = {
        "abort": _AbortProvider,
        "provider_error": _ErrorProvider,
        "budget": _BudgetProvider,
    }
    observer = _AgentStopHook()
    session_store = InMemorySessionStore()
    run_store = InMemoryRunStore()
    provider = providers[case]()
    agent = _agent(
        provider,
        session_store=session_store,
        run_store=run_store,
        hooks=[observer],
    )
    session = await agent.session(id=f"close-{case}")
    run = await run_store.create_run(session.id)
    run_record = await run_store.load_run(run.id)
    assert run_record is not None
    opts = RunOptions(budget=RunBudget(max_tokens=0)) if case == "budget" else RunOptions()
    agen = _run_loop_impl(
        session,
        "close at terminal tail",
        opts,
        run_id=run.id,
        run_record=run_record,
        resume_checkpoint=None,
    )

    terminal_event: Any = None
    async for event in agen:
        if event.type == terminal_type:
            terminal_event = event
            break
    await agen.aclose()

    assert terminal_event is not None
    assert observer.results
    result = observer.results[-1]
    assert result.subtype == subtype
    assert result.stop_reason == stop_reason
    if error_name is None:
        assert result.error is None
    else:
        assert result.error is not None
        assert result.error["name"] == error_name
