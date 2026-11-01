"""Regression tests for closing a run while a provider phase is streaming."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from tests.support.agents import make_agent


class _TrackedProvider:
    id = "tracked-close"

    def __init__(self) -> None:
        self.running = 0

    def context_window(self, model: str) -> int:
        return 100_000

    async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
        self.running += 1
        try:
            yield {"type": "message_start", "model": req.model}
            yield {"type": "text_delta", "text": "partial"}
            await asyncio.sleep(60)
            yield {"type": "message_end", "stop_reason": "end_turn"}
        finally:
            self.running -= 1


class _ParallelToolProvider:
    id = "parallel-close"

    def context_window(self, model: str) -> int:
        return 100_000

    async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
        from linch.types import Usage

        yield {"type": "message_start", "model": req.model}
        for tool_id, name in (("fast-id", "Fast"), ("slow-id", "Slow")):
            yield {"type": "tool_use_start", "id": tool_id, "name": name}
            yield {"type": "tool_use_input_delta", "id": tool_id, "json_delta": "{}"}
            yield {"type": "tool_use_end", "id": tool_id}
        yield {"type": "message_end", "stop_reason": "tool_use", "usage": Usage()}


async def test_closing_after_one_parallel_tool_end_cancels_the_other_tool() -> None:
    """Closing a tool batch cancels unfinished parallel calls before returning."""
    from linch.tools.base import ToolResult
    from tests.support.tools import StubTool, tool_registry

    slow_cancelled = asyncio.Event()

    class _FastTool(StubTool):
        async def execute(self, input: Any, ctx: Any) -> Any:
            await asyncio.sleep(0.01)
            return ToolResult(content="fast")

    class _SlowTool(StubTool):
        async def execute(self, input: Any, ctx: Any) -> Any:
            try:
                await asyncio.Event().wait()
            finally:
                slow_cancelled.set()

    agent = make_agent(
        _ParallelToolProvider(),
        tools=tool_registry(
            _FastTool("Fast", scope="read", parallel=True),
            _SlowTool("Slow", scope="read", parallel=True),
        ),
        run_store=None,
    )
    session = await agent.session()
    iterator = session.run("run both").__aiter__()
    while True:
        event = await iterator.__anext__()
        if event.type == "tool_call_end" and event.tool_name == "Fast":
            break

    await iterator.aclose()

    await asyncio.wait_for(slow_cancelled.wait(), timeout=1)


async def test_closing_after_partial_assistant_closes_provider_generator() -> None:
    """Closing the run iterator immediately closes its active provider phase."""
    provider = _TrackedProvider()
    agent = make_agent(provider, include_partial_messages=True, run_store=None)
    session = await agent.session()
    iterator = session.run("hello").__aiter__()

    while True:
        event = await iterator.__anext__()
        if event.type == "partial_assistant":
            break

    assert provider.running == 1
    await iterator.aclose()

    assert provider.running == 0
