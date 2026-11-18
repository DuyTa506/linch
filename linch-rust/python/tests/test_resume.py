"""Resume, approve and answer on Postgres after the server is killed.

Skips without `LINCH_TEST_PG_DSN`. Each test gets its own schema through the
DSN's `search_path`, dropped at the end.
"""

from __future__ import annotations

import asyncio
import os
import signal
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from scripted import config, text_turn, tool_turn

from linch_rust import (
    Allow,
    Answer,
    ConnectionClosed,
    NotSuspended,
    ResultEvent,
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


def blocking_tool(scope: Scope = "read") -> tuple[Any, asyncio.Event]:
    started = asyncio.Event()

    @tool(scope=scope)
    async def wait(q: str) -> str:
        started.set()
        await asyncio.Event().wait()
        return q

    return wait, started


async def test_a_run_resumes_on_a_new_server_after_a_kill(pg_dsn: str) -> None:
    stores = {"type": "postgres", "dsn": pg_dsn}
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
        resumed = await agent.resume(run.id)
        result = await resumed.result()
        replayed = await agent.events(run.id)

    assert not started.is_set(), "a started tool call is never run twice"
    assert isinstance(result, ResultEvent) and result.subtype == "success"
    assert result.final_text == "Recovered."
    events = [e.event for e in replayed]
    assert [e.tool_use_id for e in events if isinstance(e, ToolCallStartEvent)] == ["call_1"]
    # The interrupted call gets a placeholder result, not a tool_call_end.
    [placeholder] = [e for e in events if isinstance(e, UserEvent) and e.subtype == "tool_result"]
    [block] = placeholder.message["content"]
    assert block["tool_use_id"] == "call_1" and block["is_error"]
    assert "interrupted" in block["content"][0]["text"]


async def test_an_approve_sent_again_after_a_kill_points_to_resume(pg_dsn: str) -> None:
    stores = {"type": "postgres", "dsn": pg_dsn}
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
        resumed = await agent.resume(run.id)
        result = await resumed.result()

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
    stores = {"type": "postgres", "dsn": pg_dsn}
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
    stores = {"type": "postgres", "dsn": pg_dsn}
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
        result = await (await agent.resume(run.id)).result()
        replayed = await agent.events(run.id)

    assert not started.is_set(), "a started tool call is never run twice"
    assert isinstance(result, ResultEvent) and result.final_text == "Recovered."
    ends = [e.event for e in replayed if isinstance(e.event, ToolCallEndEvent)]
    assert [end.result["content"][0]["text"] for end in ends if end.tool_name == "AskUser"] == [
        ANSWERED
    ]
