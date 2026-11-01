from __future__ import annotations

import asyncio
from contextlib import suppress

import pytest

from linch.abort import AbortContext, any_signal


async def _wait_for(predicate, timeout: float = 1.0) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["abort", "close"])
async def test_any_signal_watcher_lifecycle(trigger: str) -> None:
    a = AbortContext()
    b = AbortContext()
    merged = any_signal(a, b)

    assert not merged.aborted
    task = merged._watch_task
    assert task is not None

    if trigger == "abort":
        a.abort()
        assert await _wait_for(lambda: merged.aborted)
    else:
        await asyncio.sleep(0)
        assert not task.done()
        merged.close()

    assert await _wait_for(lambda: task.done())
    assert not a._event._waiters
    assert not b._event._waiters


@pytest.mark.asyncio
async def test_abort_during_foreground_tool_emits_aborted_result() -> None:
    from linch import ResultEvent, ToolResult
    from linch.abort import throw_if_aborted
    from tests.support.agents import make_agent
    from tests.support.providers import ScriptedProvider, TextTurn, ToolUseTurn
    from tests.support.tools import tool_registry

    started = asyncio.Event()
    saw_abort = asyncio.Event()

    class HangUntilAbort:
        name = "hang"
        description = "Waits until the run is aborted."
        scope = "read"
        parallel = False
        input_schema: dict = {"type": "object", "properties": {}}

        def validate(self, raw: dict) -> dict:
            return dict(raw)

        def summarize(self, inp: dict) -> str:
            return "hang()"

        async def execute(self, inp: dict, ctx) -> ToolResult:
            started.set()
            await ctx.signal.wait()  # cooperative: park until aborted
            saw_abort.set()
            throw_if_aborted(ctx.signal)  # raises AbortError → aborted terminal
            return ToolResult(content="never")

    provider = ScriptedProvider([ToolUseTurn("hang", {}), TextTurn("unreached")])
    agent = make_agent(provider, tools=tool_registry(HangUntilAbort()), model="m", run_store=None)
    session = await agent.session()

    async def _abort_when_started() -> None:
        await asyncio.wait_for(started.wait(), timeout=2.0)
        session.abort()

    watcher = asyncio.ensure_future(_abort_when_started())
    try:

        async def _drive() -> list:
            return [event async for event in session.run("go")]

        events = await asyncio.wait_for(_drive(), timeout=5.0)
    finally:
        watcher.cancel()
        with suppress(asyncio.CancelledError):
            await watcher

    result = events[-1]
    assert isinstance(result, ResultEvent)
    assert result.subtype == "aborted"
    assert saw_abort.is_set()  # tool actually saw the abort, then unwound
    assert len(provider.requests) == 1  # the second turn never ran


@pytest.mark.asyncio
async def test_external_cancel_of_session_run_propagates_as_timeout() -> None:
    from collections.abc import AsyncIterator
    from typing import Any

    from linch.providers.base import BaseProvider
    from tests.support.agents import make_agent

    class HangingProvider(BaseProvider):
        id = "hanging"

        def context_window(self, model: str) -> int:
            return 128_000

        async def stream(self, req: Any) -> AsyncIterator[dict[str, Any]]:
            yield {"type": "message_start", "model": req.model}
            await asyncio.sleep(10)
            yield {"type": "text_delta", "text": "never"}  # pragma: no cover

    agent = make_agent(HangingProvider(), model="m", run_store=None)
    session = await agent.session()

    async def _drive() -> list:
        return [event async for event in session.run("go")]

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(_drive(), timeout=0.05)
