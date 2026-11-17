"""Resume, approve and answer on Postgres after the server is killed, and
run leases between two live servers.

Skips without `LINCH_TEST_PG_DSN`. Each test gets its own schema through the
DSN's `search_path`, dropped at the end. A killed server never releases the
lease of the run it drove, so the stores use the shortest lease (1 s) and a
resume on the next server waits it out.
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
import uuid
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

import pytest
from scripted import config, text_turn, tool_turn

from linch_rust import (
    Allow,
    Answer,
    ConnectionClosed,
    InitEvent,
    NotSuspended,
    ResultEvent,
    Run,
    RunLeased,
    RunTerminal,
    Scope,
    Server,
    SuspendedEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    UserEvent,
    tool,
)


@pytest.fixture
def pg_dsn() -> Iterator[str]:
    """A DSN whose `search_path` is a fresh schema."""
    dsn = os.environ.get("LINCH_TEST_PG_DSN")
    if not dsn:
        pytest.skip("LINCH_TEST_PG_DSN is not set")
    import psycopg
    from psycopg import sql

    schema = f"p12_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            separator = "&" if "?" in dsn else "?"
            yield f"{dsn}{separator}options=-c%20search_path%3D{schema}"
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def leased_stores(dsn: str) -> dict[str, Any]:
    """Postgres stores whose run leases last the shortest time allowed."""
    return {"type": "postgres", "dsn": dsn, "lease_ttl_ms": 1000}


async def once_free(start: Callable[[], Awaitable[Run]]) -> Any:
    """The result of the run `start` continues, started again while the
    killed server's lease on it lives."""
    for _ in range(20):
        run = await start()
        try:
            return await run.result()
        except RunLeased as leased:
            await asyncio.sleep(leased.expires_in or 0.2)
    raise AssertionError("the killed server's lease never ran out")


def blocking_tool(scope: Scope = "read") -> tuple[Any, asyncio.Event]:
    started = asyncio.Event()

    @tool(scope=scope)
    async def wait(q: str) -> str:
        started.set()
        await asyncio.Event().wait()
        return q

    return wait, started


async def test_a_run_resumes_on_a_new_server_after_a_kill(pg_dsn: str) -> None:
    stores = leased_stores(pg_dsn)
    wait, started = blocking_tool()
    first = config([tool_turn("call_1", "wait", {"q": "x"})], stores=stores)
    async with Server() as server:
        agent = await server.open_agent(first, tools=[wait])
        run = await agent.run("s1", "Wait for x")
        await asyncio.wait_for(started.wait(), 10)
        os.kill(server.pid, signal.SIGKILL)
        with pytest.raises(ConnectionClosed):
            await run.result()

    # Same spec and tools, so the same contract; the script holds only
    # the turn still to come.
    wait, started = blocking_tool()
    second = config([text_turn("Recovered.")], stores=stores)
    async with Server() as server:
        agent = await server.open_agent(second, tools=[wait])
        result = await once_free(lambda: agent.resume(run.id))
        replayed = await agent.events(run.id)

    assert not started.is_set(), "a started tool call is never run twice"
    assert isinstance(result, ResultEvent) and result.subtype == "success"
    assert result.final_text == "Recovered."
    events = [e.event for e in replayed]
    assert [e.tool_use_id for e in events if isinstance(e, ToolCallStartEvent)] == ["call_1"]
    # The interrupted call is closed with a `tool_call_end` that is an error,
    # and the same result is paired in the session.
    [ended] = [e for e in events if isinstance(e, ToolCallEndEvent)]
    assert ended.tool_use_id == "call_1" and ended.result["is_error"]
    # Each call of the run starts with an `init`; the resume's says so.
    assert [e.resumed for e in events if isinstance(e, InitEvent)] == [False, True]
    [placeholder] = [e for e in events if isinstance(e, UserEvent) and e.subtype == "tool_result"]
    [block] = placeholder.message["content"]
    assert block["tool_use_id"] == "call_1" and block["is_error"]
    assert "interrupted" in block["content"][0]["text"]


async def test_an_approve_sent_again_after_a_kill_points_to_resume(pg_dsn: str) -> None:
    stores = leased_stores(pg_dsn)
    wait, started = blocking_tool(scope="write")
    first = config([tool_turn("call_1", "wait", {"q": "x"})], stores=stores)
    async with Server() as server:
        agent = await server.open_agent(first, tools=[wait])
        run = await agent.run("s1", "Wait for x")
        assert isinstance(await run.result(), SuspendedEvent)
        approved = await agent.approve(run.id, {"call_1": Allow()})
        await asyncio.wait_for(started.wait(), 10)
        os.kill(server.pid, signal.SIGKILL)
        with pytest.raises(ConnectionClosed):
            await approved.result()

    # The answer is committed, so the run no longer waits for one: the
    # host that sends the approve again is told to resume instead.
    wait, started = blocking_tool(scope="write")
    second = config([text_turn("Recovered.")], stores=stores)
    async with Server() as server:
        agent = await server.open_agent(second, tools=[wait])
        again = await agent.approve(run.id, {"call_1": Allow()})
        with pytest.raises(NotSuspended) as refused:
            await again.result()
        result = await once_free(lambda: agent.resume(run.id))

    assert "resume" in refused.value.message
    assert not started.is_set(), "a started tool call is never run twice"
    assert isinstance(result, ResultEvent) and result.final_text == "Recovered."


ASK = {
    "questions": [
        {
            "id": "db",
            "header": "Database",
            "question": "Which database?",
            "options": [
                {"label": "Postgres", "description": "durable"},
                {"label": "Memory", "description": "for tests"},
            ],
        }
    ]
}
ANSWERED = '{"answers":{"db":"Postgres"}}'


async def test_an_answer_reaches_a_run_suspended_before_a_kill(pg_dsn: str) -> None:
    stores = leased_stores(pg_dsn)
    first = config([tool_turn("ask_1", "AskUser", ASK)], stores=stores, ask_user=True)
    async with Server() as server:
        agent = await server.open_agent(first)
        run = await agent.run("s1", "Set up storage")
        assert isinstance(await run.result(), SuspendedEvent)
        os.kill(server.pid, signal.SIGKILL)

    second = config([text_turn("Postgres it is.")], stores=stores, ask_user=True)
    async with Server() as server:
        agent = await server.open_agent(second)
        answered = await agent.answer(run.id, {"ask_1": Answer({"db": "Postgres"})})
        result = await answered.result()
        late = await agent.answer(run.id, {"ask_1": Answer({"db": "Memory"})})
        with pytest.raises(RunTerminal):
            await late.result()
        replayed = await agent.events(run.id)

    assert isinstance(result, ResultEvent) and result.final_text == "Postgres it is."
    ends = [e.event for e in replayed if isinstance(e.event, ToolCallEndEvent)]
    assert [end.result["content"][0]["text"] for end in ends] == [ANSWERED]


async def test_an_answer_sent_again_after_a_kill_points_to_resume(pg_dsn: str) -> None:
    stores = leased_stores(pg_dsn)
    wait, started = blocking_tool()
    turns = [tool_turn("ask_1", "AskUser", ASK), tool_turn("call_1", "wait", {"q": "x"})]
    first = config(turns, stores=stores, ask_user=True)
    async with Server() as server:
        agent = await server.open_agent(first, tools=[wait])
        run = await agent.run("s1", "Set up storage")
        assert isinstance(await run.result(), SuspendedEvent)
        answered = await agent.answer(run.id, {"ask_1": Answer({"db": "Postgres"})})
        await asyncio.wait_for(started.wait(), 10)
        os.kill(server.pid, signal.SIGKILL)
        with pytest.raises(ConnectionClosed):
            await answered.result()

    # The answer is committed: sending it again is refused, a resume goes
    # on, and the model saw the first answer, once.
    wait, started = blocking_tool()
    second = config([text_turn("Recovered.")], stores=stores, ask_user=True)
    async with Server() as server:
        agent = await server.open_agent(second, tools=[wait])
        again = await agent.answer(run.id, {"ask_1": Answer({"db": "Memory"})})
        with pytest.raises(NotSuspended):
            await again.result()
        result = await once_free(lambda: agent.resume(run.id))
        replayed = await agent.events(run.id)

    assert not started.is_set(), "a started tool call is never run twice"
    assert isinstance(result, ResultEvent) and result.final_text == "Recovered."
    ends = [e.event for e in replayed if isinstance(e.event, ToolCallEndEvent)]
    assert [end.result["content"][0]["text"] for end in ends if end.tool_name == "AskUser"] == [
        ANSWERED
    ]


async def test_a_second_server_cannot_take_a_live_run(pg_dsn: str) -> None:
    stores = {"type": "postgres", "dsn": pg_dsn}
    wait, started = blocking_tool()
    first = config([tool_turn("call_1", "wait", {"q": "x"})], stores=stores)
    second = config([text_turn("Taken.")], stores=stores)
    async with Server() as one, Server() as two:
        holder = await one.open_agent(first, tools=[wait])
        run = await holder.run("s1", "Wait for x")
        await asyncio.wait_for(started.wait(), 10)
        other, _ = blocking_tool()
        agent = await two.open_agent(second, tools=[other])
        taken = await agent.resume(run.id)
        with pytest.raises(RunLeased) as refused:
            await taken.result()
        await run.cancel()
        await run.result()

    assert refused.value.run_id == run.id
    assert refused.value.expires_in is not None and 20 < refused.value.expires_in <= 30


async def test_a_refusal_names_the_holder_by_an_opaque_stable_id(pg_dsn: str) -> None:
    # `RunLeased.owner` tells an operator which server drives the run, and holds no host
    # name, path or pid: 8 hex characters, the same for every run of one server.
    stores = {"type": "postgres", "dsn": pg_dsn}
    async with Server() as one, Server() as three, Server() as two:
        for server, run_id in ((one, "held-1"), (three, "held-3")):
            wait, started = blocking_tool()
            held = config([tool_turn("call_1", "wait", {"q": "x"})], stores=stores)
            agent = await server.open_agent(held, tools=[wait])
            await agent.run(f"s-{run_id}", "Wait for x", run_id=run_id)
            await asyncio.wait_for(started.wait(), 10)
        other, _ = blocking_tool()
        probe = await two.open_agent(config([text_turn("Taken.")], stores=stores), tools=[other])
        owners = []
        for run_id in ("held-1", "held-3", "held-1"):
            with pytest.raises(RunLeased) as refused:
                await (await probe.resume(run_id)).result()
            owners.append(refused.value.owner)

    assert all(re.fullmatch(r"[0-9a-f]{8}", owner) for owner in owners), owners
    assert owners[0] == owners[2] and owners[0] != owners[1], owners


async def test_a_server_that_exits_frees_its_runs_at_once(pg_dsn: str) -> None:
    # The default lease: only the release on exit makes the run free now.
    stores = {"type": "postgres", "dsn": pg_dsn}
    wait, started = blocking_tool()
    first = config([tool_turn("call_1", "wait", {"q": "x"})], stores=stores)
    async with Server() as server:
        agent = await server.open_agent(first, tools=[wait])
        run = await agent.run("s1", "Wait for x")
        await asyncio.wait_for(started.wait(), 10)

    wait, started = blocking_tool()
    second = config([text_turn("Recovered.")], stores=stores)
    async with Server() as server:
        agent = await server.open_agent(second, tools=[wait])
        result = await (await agent.resume(run.id)).result()

    assert isinstance(result, ResultEvent) and result.final_text == "Recovered."


async def test_a_suspended_run_is_cancelled_by_another_server(pg_dsn: str) -> None:
    stores = leased_stores(pg_dsn)
    wait, started = blocking_tool(scope="write")
    first = config([tool_turn("call_1", "wait", {"q": "x"})], stores=stores)
    async with Server() as server:
        agent = await server.open_agent(first, tools=[wait])
        run = await agent.run("s1", "Wait for x")
        assert isinstance(await run.result(), SuspendedEvent)

    # Another config, so another contract: a cancel does not check it.
    second = config([text_turn("Next.")], mode="skip_dangerous", stores=stores)
    async with Server() as server:
        agent = await server.open_agent(second, tools=[wait])
        assert await agent.cancel(run.id)
        events = [e.event for e in await agent.events(run.id)]
        late = await agent.approve(run.id, {"call_1": Allow()})
        with pytest.raises(RunTerminal):
            await late.result()
        assert not await agent.cancel(run.id)

    assert not started.is_set()
    [end] = [e for e in events if isinstance(e, ToolCallEndEvent)]
    assert end.is_error and end.result["content"][0]["text"] == "Tool call aborted"
    result = events[-1]
    assert isinstance(result, ResultEvent) and result.subtype == "aborted"
