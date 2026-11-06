"""Regression coverage for mutable state crossing extracted loop phases."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from tests.support.agents import make_agent


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


def _agent(
    provider: Any,
    *,
    session_store: Any,
    run_store: Any,
    hooks: list[Any] | None = None,
) -> Any:
    from linch.config import FeatureFlags
    from linch.loop_guard import LoopGuard
    from tests.support.tools import StubTool, tool_registry

    return make_agent(
        provider,
        model="test-model",
        tools=tool_registry(StubTool("Repeat")),
        permissions={"mode": "skip-dangerous"},
        session_store=session_store,
        run_store=run_store,
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        loop_guard=LoopGuard(max_identical_tool_calls=2, force_final_answer=True),
        hooks=hooks,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "terminal_type", "subtype", "stop_reason", "error_name"),
    [
        ("abort", "result", "aborted", "error", None),
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
    from linch.profiles import _RunBinding

    session._run_bindings[run.id] = _RunBinding(session, run.id, agent._run_config)
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
    await session._run_bindings[run.id].release()

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
