"""Host tools: run in this process, errors the model reads, and cancellation."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from scripted import config, text_turn, tool_turn

from linch_rust import ResultEvent, Server, ToolCallEndEvent, tool


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
