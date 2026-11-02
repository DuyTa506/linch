"""An interrupted in-process run keeps its session busy until the host settles it."""

import asyncio

import pytest

from linch import RunOptions, SessionBusyError
from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn, ToolUseTurn
from tests.support.tools import StubTool, tool_registry


async def _collect(session, prompt, run_id):
    return [event async for event in session.run(prompt, RunOptions(run_id=run_id))]


async def _start_blocked_run(script):
    started = asyncio.Event()

    async def block(raw, ctx):
        started.set()
        await asyncio.sleep(0.2)

    agent = make_agent(
        ScriptedProvider([ToolUseTurn("slow", {}), *script]),
        tools=tool_registry(StubTool("slow", on_execute=block)),
    )
    session = await agent.session()
    task = asyncio.create_task(_collect(session, "first", "r1"))
    await started.wait()
    return agent, session, task


async def test_cancelled_run_blocks_until_marked_failed() -> None:
    agent, session, task = await _start_blocked_run([TextTurn("second")])
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    with pytest.raises(SessionBusyError) as busy:
        await _collect(session, "second", "r2")
    assert busy.value.run_id == "r1"

    await agent.run_store.mark_failed("r1", error={"message": "client disconnected"})
    assert (await _collect(session, "second", "r3"))[-1].final_text == "second"
    await agent.close()


async def test_abort_frees_the_session_without_host_cleanup() -> None:
    agent, session, task = await _start_blocked_run([TextTurn("second")])
    session.abort()
    await task

    assert (await _collect(session, "second", "r2"))[-1].final_text == "second"
    await agent.close()
