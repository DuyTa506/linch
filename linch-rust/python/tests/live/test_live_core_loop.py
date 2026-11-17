"""Live: the loop guard ends a stuck run; cancel ends a streaming or suspended one."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from live_core_support import (
    PROVIDERS,
    Live,
    drive,
    live_server,
    make_config,
    needs_pg,
    pg_stores,
    retry_once,
)

from linch_rust import (
    ErrorEvent,
    LoopGuardEvent,
    PartialAssistantEvent,
    ResultEvent,
    SuspendedEvent,
    tool,
)

pytestmark = pytest.mark.live


def run_status(namespace: str, run_id: str) -> str:
    import psycopg
    from live_core_support import PG_DSN

    assert PG_DSN
    with psycopg.connect(PG_DSN) as conn:
        row = conn.execute(
            "SELECT status FROM linch_runs WHERE namespace = %s AND run_id = %s",
            (namespace, run_id),
        ).fetchone()
    assert row is not None
    return str(row[0])


@needs_pg
@pytest.mark.parametrize("live", PROVIDERS)
@retry_once
async def test_the_loop_guard_ends_a_run_that_repeats_the_same_call(live: Live) -> None:
    calls: list[int] = []

    @tool(scope="read")
    async def ping(n: int) -> str:
        """Ping the service."""
        calls.append(n)
        return "pong"

    config = make_config(live, live.spec(max_turns=12), stores=pg_stores())
    async with live_server() as server:
        agent = await server.open_agent(config, tools=[ping])
        run = await agent.run(
            "s1",
            "Call the ping tool with n=1. After every result call ping with n=1 again. "
            "Never answer with text; just keep calling ping with n=1.",
        )
        trace = await drive(agent, run)
        durable = await agent.events(run.id)

    [guard] = trace.of(LoopGuardEvent)
    assert guard.reason == "repeated_tool_call"
    assert len(calls) == 3  # the call that reaches the limit runs first (docs)
    [error] = trace.of(ErrorEvent)
    assert error.error["name"] == "LoopGuardError"
    assert trace.result.subtype == "error"
    # Durable: the guard event and the failed run are in the store.
    assert any(isinstance(e.event, LoopGuardEvent) for e in durable)
    assert isinstance(durable[-1].event, ResultEvent)
    assert run_status(config["namespace"], run.id) == "failed"


@needs_pg
@pytest.mark.parametrize("live", PROVIDERS)
@retry_once
async def test_cancel_mid_stream_ends_the_run_aborted(live: Live) -> None:
    config = make_config(live, stores=pg_stores())
    async with live_server() as server:
        agent = await server.open_agent(config)
        run = await agent.run("s1", "Count from 1 to 400, one number per line, no other text.")
        seen = 0
        async with asyncio.timeout(120):
            async for envelope in run:
                if isinstance(envelope.event, PartialAssistantEvent):
                    seen += 1
                if seen == 3:
                    break
            assert await run.cancel() is True
            result = await run.result()
        durable = await agent.events(run.id)
        # The session is free again: a new run starts at once.
        followup = await agent.run("s1", "Reply with the word ok.")
        assert (await followup.result()).subtype == "success"  # type: ignore[union-attr]

    assert isinstance(result, ResultEvent)
    assert result.subtype == "aborted"
    assert isinstance(durable[-1].event, ResultEvent) and durable[-1].event.subtype == "aborted"
    assert run_status(config["namespace"], run.id) == "aborted"


@needs_pg
@pytest.mark.parametrize("live", PROVIDERS[:1])
@retry_once
async def test_cancelling_a_run_waiting_for_approval_frees_the_session(live: Live) -> None:
    ran: list[str] = []

    @tool(scope="write")
    async def save_note(text: str) -> str:
        """Save a note."""
        ran.append(text)
        return "saved"

    config: Any = make_config(live, stores=pg_stores())
    async with live_server() as server:
        agent = await server.open_agent(config, tools=[save_note])
        run = await agent.run("s1", "Call save_note with text 'x'.")
        async for _ in run:
            pass
        assert isinstance(await run.result(), SuspendedEvent)
        assert run_status(config["namespace"], run.id) == "awaiting_approval"
        assert await agent.cancel(run.id) is True
        assert run_status(config["namespace"], run.id) == "aborted"
        followup = await agent.run("s1", "Reply with the word ok.")
        assert (await followup.result()).subtype == "success"  # type: ignore[union-attr]
    assert ran == []
