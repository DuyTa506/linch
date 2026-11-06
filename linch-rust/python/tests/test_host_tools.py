"""Host tools: run in this process, errors the model reads, and cancellation."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from scripted import config, text_turn, tool_turn

from linch_rust import (
    Agent,
    Principal,
    ResultEvent,
    Server,
    ToolCallEndEvent,
    ToolContext,
    ToolOutput,
    tool,
)


def tool_ends(envelopes: list[Any]) -> list[ToolCallEndEvent]:
    return [e.event for e in envelopes if isinstance(e.event, ToolCallEndEvent)]


def result_text(end: ToolCallEndEvent) -> str:
    return "".join(part.get("text", "") for part in end.result["content"])


async def test_a_host_tool_runs_in_the_client_and_the_model_reads_its_value() -> None:
    seen: list[str] = []

    @tool(scope="read")
    async def lookup(email: str) -> dict[str, str]:
        """Find a customer by email."""
        seen.append(email)
        return {"name": "Ada", "email": email}

    turns = [tool_turn("call_1", "lookup", {"email": "ada@example.com"}), text_turn("Found.")]
    async with Server() as server:
        agent = await server.open_agent(config(turns), tools=[lookup])
        run = await agent.run("s1", "Who is ada@example.com?")
        envelopes = [e async for e in run]
        result = await run.result()

    assert seen == ["ada@example.com"]
    [end] = tool_ends(envelopes)
    assert end.tool_use_id == "call_1" and not end.is_error
    assert json.loads(result_text(end)) == {"name": "Ada", "email": "ada@example.com"}
    assert isinstance(result, ResultEvent) and result.final_text == "Found."


async def test_a_host_tool_that_declares_a_context_learns_which_call_it_serves() -> None:
    seen: list[ToolContext] = []

    @tool(scope="read")
    async def lookup(email: str, ctx: ToolContext) -> str:
        """Find a customer by email."""
        seen.append(ctx)
        return email

    turns = [tool_turn("call_1", "lookup", {"email": "ada@example.com"}), text_turn("Found.")]
    principal: Principal = {"tenant": "acme", "user": "ada", "roles": ["support"]}
    async with Server() as server:
        agent = await server.open_agent(config(turns), tools=[lookup])
        run = await agent.run("s1", "Who is ada@example.com?", run_id="r1", principal=principal)
        result = await run.result()

    [ctx] = seen
    assert (ctx.session_id, ctx.run_id, ctx.tool_use_id) == ("s1", "r1", "call_1")
    assert ctx.principal == principal
    assert ctx.idempotency_key == "r1:call_1"
    assert isinstance(result, ResultEvent) and result.final_text == "Found."


async def test_a_host_tool_returns_metadata_the_host_reads_and_the_model_does_not() -> None:
    @tool(scope="read")
    async def lookup(email: str) -> ToolOutput:
        """Find a customer by email."""
        return ToolOutput({"name": "Ada"}, metadata={"source": "crm", "ms": 4})

    turns = [tool_turn("call_1", "lookup", {"email": "ada@example.com"}), text_turn("Found.")]
    async with Server() as server:
        agent = await server.open_agent(config(turns), tools=[lookup])
        envelopes = [e async for e in await agent.run("s1", "Who is ada@example.com?")]

    [end] = tool_ends(envelopes)
    assert json.loads(result_text(end)) == {"name": "Ada"}
    assert end.metadata == {"source": "crm", "ms": 4}
    assert "crm" not in result_text(end)


async def test_a_raising_host_tool_is_a_tool_error_the_model_reads() -> None:
    @tool(scope="read")
    async def lookup(email: str) -> str:
        raise ValueError(f"no customer {email}")

    turns = [tool_turn("call_1", "lookup", {"email": "x@y.z"}), text_turn("Sorry.")]
    async with Server() as server:
        agent = await server.open_agent(config(turns), tools=[lookup])
        run = await agent.run("s1", "Look up x@y.z")
        envelopes = [e async for e in run]

    [end] = tool_ends(envelopes)
    assert end.is_error
    assert "ValueError: no customer x@y.z" in result_text(end)


def hanging(timeout: float | None = None) -> tuple[Any, asyncio.Event, asyncio.Event]:
    """A host tool that waits forever, and events set when it starts and is cancelled."""
    started, cancelled = asyncio.Event(), asyncio.Event()

    @tool(scope="read", timeout=timeout)
    async def wait(q: str) -> str:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return q

    return wait, started, cancelled


async def test_cancelling_a_run_cancels_its_host_tool_task() -> None:
    wait, started, cancelled = hanging()
    turns = [tool_turn("call_1", "wait", {"q": "x"}), text_turn("unused")]
    async with Server() as server:
        agent = await server.open_agent(config(turns), tools=[wait])
        run = await agent.run("s1", "Wait")
        await asyncio.wait_for(started.wait(), 10)
        assert await run.cancel()
        result = await run.result()
        await asyncio.wait_for(cancelled.wait(), 10)
        assert not await run.cancel()

    assert isinstance(result, ResultEvent) and result.subtype == "aborted"


def cancel_before_answer(agent: Agent, call: asyncio.Task[Any], run_id: str) -> None:
    """Cancel `call` as the answer naming `run_id` arrives, before it is read."""
    conn = agent._conn  # pyright: ignore[reportPrivateUsage]
    dispatch = conn._dispatch  # pyright: ignore[reportPrivateUsage]

    def cancel_first(message: dict[str, Any]) -> None:
        result = message.get("result")
        if isinstance(result, dict) and result.get("run_id") == run_id:
            call.cancel()
        dispatch(message)

    conn._dispatch = cancel_first  # pyright: ignore[reportPrivateUsage]


async def test_a_run_call_cancelled_before_its_answer_cancels_the_run() -> None:
    wait, _, _ = hanging()
    turns = [tool_turn("call_1", "wait", {"q": "x"}), text_turn("unused")]
    async with Server() as server:
        agent = await server.open_agent(config(turns), tools=[wait])
        call = asyncio.create_task(agent.run("s1", "Wait", run_id="r1"))
        cancel_before_answer(agent, call, "r1")
        with pytest.raises(asyncio.CancelledError):
            await call
        # The host tool never returns: only a cancel ends the run.
        async with asyncio.timeout(10):
            while not (events := await agent.events("r1")) or not isinstance(
                events[-1].event, ResultEvent
            ):
                await asyncio.sleep(0.01)
        await agent.close()

    result = events[-1].event
    assert isinstance(result, ResultEvent) and result.subtype == "aborted"


async def test_closing_an_agent_waits_for_the_runs_of_its_cancelled_calls() -> None:
    wait, started, cancelled = hanging()
    turns = [tool_turn("call_1", "wait", {"q": "x"}), text_turn("unused")]
    async with Server() as server:
        agent = await server.open_agent(config(turns), tools=[wait])
        call = asyncio.create_task(agent.run("s1", "Wait", run_id="r1"))
        cancel_before_answer(agent, call, "r1")
        with pytest.raises(asyncio.CancelledError):
            await call
        await asyncio.wait_for(agent.close(), 10)
        if started.is_set():
            await asyncio.wait_for(cancelled.wait(), 10)


async def test_a_host_tool_timeout_cancels_its_task() -> None:
    wait, _, cancelled = hanging(timeout=0.2)
    turns = [tool_turn("call_1", "wait", {"q": "x"}), text_turn("Gave up.")]
    async with Server() as server:
        agent = await server.open_agent(config(turns), tools=[wait])
        run = await agent.run("s1", "Wait")
        envelopes = [e async for e in run]
        await asyncio.wait_for(cancelled.wait(), 10)

    [end] = tool_ends(envelopes)
    assert end.is_error and "timed out" in result_text(end)
