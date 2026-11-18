"""Steering: `Agent.steer` and `Run.steer` send text to a run that is going on,
a run waiting for an approval, and, on Postgres, a run whose server was killed
(skips without `LINCH_TEST_PG_DSN`, as `test_resume.py`)."""

from __future__ import annotations

import asyncio
import os
import signal
import uuid
from collections.abc import Iterator

import pytest
from scripted import config, text_turn, tool_turn

from linch_rust import (
    Allow,
    ConnectionClosed,
    Envelope,
    InvalidSteer,
    LimitReached,
    ResultEvent,
    RunLeased,
    RunNotFound,
    RunTerminal,
    Server,
    SuspendedEvent,
    ToolCallStartEvent,
    UserEvent,
    tool,
)


def steers(envelopes: list[Envelope]) -> list[tuple[str, str]]:
    """`(steer_id, text)` of each steer a run delivered, in order."""
    found: list[tuple[str, str]] = []
    for envelope in envelopes:
        event = envelope.event
        if isinstance(event, UserEvent) and event.subtype == "alignment":
            assert event.steer_id is not None
            found.append((event.steer_id, event.message["content"][0]["text"]))
    return found


async def test_a_steer_sent_during_a_tool_call_is_read_after_its_results() -> None:
    started, release = asyncio.Event(), asyncio.Event()

    @tool(scope="read")
    async def wait(q: str) -> str:
        started.set()
        await release.wait()
        return q

    turns = [tool_turn("call_1", "wait", {"q": "x"}), text_turn("Tabs it is.")]
    async with Server() as server:
        agent = await server.open_agent(config(turns), tools=[wait])
        run = await agent.run("s1", "Format it")
        await asyncio.wait_for(started.wait(), 10)
        # The run answers the steer once it is past the tool call.
        sent = asyncio.ensure_future(run.steer("use tabs, not spaces", steer_id="st-1"))
        await asyncio.sleep(0.2)
        assert not sent.done()
        release.set()
        assert await asyncio.wait_for(sent, 10) == "st-1"
        envelopes = [e async for e in run]
        replayed = await agent.events(run.id)

    assert steers(envelopes) == [("st-1", "use tabs, not spaces")]
    assert steers(replayed) == steers(envelopes), "durable"
    result = envelopes[-1].event
    assert isinstance(result, ResultEvent) and result.final_text == "Tabs it is."
    user_roles = [e.event.message["role"] for e in envelopes if isinstance(e.event, UserEvent)]
    assert user_roles == ["user", "user", "user"], "the prompt, the results, the steer"


async def test_a_suspended_run_takes_a_steer_and_delivers_it_once_approved() -> None:
    @tool(scope="write")
    async def save(text: str) -> str:
        return "saved"

    turns = [tool_turn("call_1", "save", {"text": "x"}), text_turn("Done.")]
    async with Server() as server:
        agent = await server.open_agent(config(turns), tools=[save])
        run = await agent.run("s1", "Save")
        assert isinstance(await run.result(), SuspendedEvent)
        first = await agent.steer(run.id, "mind the tabs")
        again = await agent.steer(run.id, "mind the tabs", steer_id=first)
        assert again == first, "a resent id is taken once"
        with pytest.raises(InvalidSteer):
            await agent.steer(run.id, "   ")
        with pytest.raises(InvalidSteer):
            await agent.steer(run.id, "x" * (16 * 1024 + 1))
        with pytest.raises(RunNotFound):
            await agent.steer("no-such-run", "hello")
        for n in range(15):
            await agent.steer(run.id, "more", steer_id=f"f{n}")
        with pytest.raises(LimitReached) as full:
            await agent.steer(run.id, "one too many")
        assert full.value.kind == "steers"

        approved = await agent.approve(run.id, {"call_1": Allow()})
        envelopes = [e async for e in approved]
        delivered = steers(envelopes)
        assert [text for _, text in delivered][0] == "mind the tabs"
        assert len(delivered) == 16
        with pytest.raises(RunTerminal):
            await agent.steer(run.id, "too late")


@pytest.fixture
def pg_dsn() -> Iterator[str]:
    dsn = os.environ.get("LINCH_TEST_PG_DSN")
    if not dsn:
        pytest.skip("LINCH_TEST_PG_DSN is not set")
    import psycopg
    from psycopg import sql

    schema = f"steer_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            separator = "&" if "?" in dsn else "?"
            yield f"{dsn}{separator}options=-c%20search_path%3D{schema}"
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


async def test_a_steer_survives_a_server_kill_and_is_read_once(pg_dsn: str) -> None:
    stores = {"type": "postgres", "dsn": pg_dsn, "lease_ttl_ms": 1000}

    @tool(scope="write")
    async def save(text: str) -> str:
        return "saved"

    turns = [tool_turn("call_1", "save", {"text": "x"}), text_turn("Done.")]
    async with Server() as server:
        agent = await server.open_agent(config(turns, stores=stores), tools=[save])
        run = await agent.run("s1", "Save")
        assert isinstance(await run.result(), SuspendedEvent)
        await agent.steer(run.id, "mind the tabs", steer_id="st-1")
        # The steer is in the journal: kill the server before anything reads it.
        os.kill(server.pid, signal.SIGKILL)
        with pytest.raises(ConnectionClosed):
            await agent.cancel(run.id)

    async with Server() as server:
        agent = await server.open_agent(config(turns, stores=stores), tools=[save])
        for _ in range(20):
            try:
                approved = await agent.approve(run.id, {"call_1": Allow()})
                envelopes = [e async for e in approved]
                break
            except RunLeased as leased:
                await asyncio.sleep(leased.expires_in or 0.2)
        else:
            raise AssertionError("the lease never ran out")
        replayed = await agent.events(run.id)

    assert steers(envelopes) == [("st-1", "mind the tabs")]
    starts = [e.event for e in replayed if isinstance(e.event, ToolCallStartEvent)]
    assert [s.tool_use_id for s in starts] == ["call_1"]
    assert steers(replayed) == steers(envelopes), "read once, durable"
