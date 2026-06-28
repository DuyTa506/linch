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
async def test_merged_aborts_when_input_aborts_and_watcher_terminates() -> None:
    a = AbortContext()
    b = AbortContext()
    merged = any_signal(a, b)

    assert not merged.aborted

    a.abort()

    # merged should become aborted shortly after an input aborts
    assert await _wait_for(lambda: merged.aborted)

    # the internal watcher must terminate (not park forever / leak)
    assert merged._watch_task is not None
    assert await _wait_for(lambda: merged._watch_task.done())


@pytest.mark.asyncio
async def test_close_cancels_watcher_when_no_abort_fires() -> None:
    a = AbortContext()
    b = AbortContext()
    merged = any_signal(a, b)

    task = merged._watch_task
    assert task is not None

    # let the watcher start parking on the input events
    await asyncio.sleep(0)
    assert not task.done()

    # cleanup path must release the watcher
    merged.close()

    assert await _wait_for(lambda: task.done())

    # no pending Event.wait futures should dangle: the input events have no
    # remaining waiters once the watcher is cleaned up.
    assert not a._event._waiters
    assert not b._event._waiters


@pytest.mark.asyncio
async def test_abort_during_foreground_tool_emits_aborted_result() -> None:
    """Embedding gate: aborting while a foreground tool is mid-execution ends the
    run with an ``aborted`` result. The tool observes ``ctx.signal`` (the merged
    signal really is wired to ``session.abort()``), and the generator finishes
    cleanly rather than hanging."""
    from linch import Agent, ResultEvent, ToolResult
    from linch.abort import throw_if_aborted
    from linch.evals import ScriptedProvider, TextTurn, ToolUseTurn
    from linch.sessions import InMemorySessionStore
    from linch.tools import ToolRegistry

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

    tools = ToolRegistry()
    tools.add(HangUntilAbort())
    provider = ScriptedProvider(
        [ToolUseTurn(tool_name="hang", tool_input={}), TextTurn(text="unreached")]
    )
    agent = Agent(
        model="m",
        provider=provider,
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        cwd=".",
        tools=tools,
    )
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
    assert provider._index == 1  # the second turn never ran
