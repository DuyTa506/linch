"""Live: steering an in-flight run.

Python Linch has `Session.align(prompt)` (`src/linch/session.py:565`, drained by
`drain_alignment` at `src/linch/loop/runtime.py:327`): text queued while a run is
active is logged as a user message of subtype `alignment` at the next turn
boundary, snapshotted into every checkpoint and restored on resume.

linch-rust steers through `Run.steer` / `Agent.steer` (RPC `run.steer`): the text is
a user message logged after the tool results of the batch in progress and before
the next model call, reported as a `user` event of subtype `alignment`. The
mailbox (`mailbox: true`) is a second way in: a run drains its session's inbox
at its start and after each tool batch (`crates/linch-core/src/runner/inbox.rs`).
These tests expect:

* `Run.steer` while a tool call is in flight: the call answers only after the tool
  batch ends; the model then follows the instruction; an `alignment` user event
  carries the returned id;
* a row inserted into `linch_mailbox` by the host (Postgres stores) is applied at
  the next turn boundary and acked;
* a `context_build` callback that reads a host-side queue feeds the next request
  (per request, not logged).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from live_core_support import (
    PG_DSN,
    PROVIDERS,
    Live,
    live_server,
    make_config,
    message_text,
    needs_pg,
    pg_stores,
    retry_once,
)

from linch_rust import Run, UserEvent, callback, tool

pytestmark = pytest.mark.live

PROMPT = (
    "Call the slow_task tool. When it returns, follow any new instruction you received while "
    "it ran, then answer in one short sentence."
)


def mailbox_rows(namespace: str, recipient: str) -> int:
    import psycopg

    assert PG_DSN
    with psycopg.connect(PG_DSN) as conn:
        row = conn.execute(
            "SELECT count(*) FROM linch_mailbox WHERE namespace = %s AND recipient = %s",
            (namespace, recipient),
        ).fetchone()
    assert row is not None
    return int(row[0])


def insert_message(namespace: str, recipient: str, content: str) -> None:
    import psycopg

    assert PG_DSN
    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO linch_mailbox (namespace, recipient, id, sender, content, kind) "
            "VALUES (%s, %s, %s, 'operator', %s, 'message')",
            (namespace, recipient, uuid.uuid4().hex, content),
        )


async def collect(run: Run, into: list[Any], started: asyncio.Event) -> None:
    async for envelope in run:
        into.append(envelope)
        if type(envelope.event).__name__ == "ToolCallStartEvent":
            started.set()


@needs_pg
@pytest.mark.parametrize("live", PROVIDERS)
@retry_once
async def test_a_host_message_in_the_mailbox_is_applied_at_the_next_turn_boundary(
    live: Live,
) -> None:
    release = asyncio.Event()
    started = asyncio.Event()

    @tool(scope="read")
    async def slow_task() -> str:
        """A long task."""
        started.set()
        await release.wait()
        return "task finished"

    config = make_config(live, live.spec(max_turns=5), mailbox=True, stores=pg_stores())
    namespace = config["namespace"]
    async with live_server() as server:
        agent = await server.open_agent(config, tools=[slow_task])
        run = await agent.run("s1", PROMPT)
        envelopes: list[Any] = []
        pump = asyncio.create_task(collect(run, envelopes, started))
        async with asyncio.timeout(120):
            await started.wait()
            # The run is active, inside its tool batch: steer it.
            insert_message(
                namespace,
                "s1",
                "New instruction: end your answer with the word KIWI-42.",
            )
            assert mailbox_rows(namespace, "s1") == 1
            release.set()
            await pump
        result = await run.result()

    peers = [
        message_text(e.event.message)
        for e in envelopes
        if isinstance(e.event, UserEvent) and "<peer-message>" in str(e.event.message)
    ]
    assert peers and "KIWI-42" in str(peers[0])
    assert "<from>operator</from>" in str(peers[0])
    assert "KIWI-42" in (result.final_text or "")  # type: ignore[union-attr]
    assert mailbox_rows(namespace, "s1") == 0  # acked once it was logged


@pytest.mark.parametrize("live", PROVIDERS)
@retry_once
async def test_a_context_build_callback_can_feed_a_host_queue_into_the_next_request(
    live: Live,
) -> None:
    release = asyncio.Event()
    started = asyncio.Event()
    queue: list[str] = []

    @tool(scope="read")
    async def slow_task() -> str:
        """A long task."""
        started.set()
        await release.wait()
        return "task finished"

    @callback("context_build", "steer-queue", 1)
    async def feed(_request: dict[str, Any]) -> dict[str, Any]:
        blocks = [{"text": text} for text in queue]
        return {"blocks": blocks} if blocks else {}

    async with live_server() as server:
        agent = await server.open_agent(
            make_config(live, live.spec(max_turns=5)), tools=[slow_task], callbacks=[feed]
        )
        run = await agent.run("s1", PROMPT)
        envelopes: list[Any] = []
        pump = asyncio.create_task(collect(run, envelopes, started))
        async with asyncio.timeout(120):
            await started.wait()
            queue.append("New instruction: end your answer with the word MANGO-17.")
            release.set()
            await pump
        result = await run.result()

    assert "MANGO-17" in (result.final_text or "")  # type: ignore[union-attr]
    # Unlike Python's `alignment` message, this is not in the session log.
    assert not any(
        "MANGO-17" in str(e.event.message) for e in envelopes if isinstance(e.event, UserEvent)
    )


@pytest.mark.parametrize("live", PROVIDERS)
@retry_once
async def test_run_steer_during_a_tool_call_is_followed_and_logged_as_an_alignment(
    live: Live,
) -> None:
    release = asyncio.Event()
    started = asyncio.Event()

    @tool(scope="read")
    async def slow_task() -> str:
        """A long task."""
        started.set()
        await release.wait()
        return "task finished"

    async with live_server() as server:
        agent = await server.open_agent(
            make_config(live, live.spec(max_turns=5)), tools=[slow_task]
        )
        run = await agent.run("s1", PROMPT)
        envelopes: list[Any] = []
        pump = asyncio.create_task(collect(run, envelopes, started))
        async with asyncio.timeout(180):
            await started.wait()
            # The call answers once the run has written the steer, at its next step
            # boundary, which is after the tool batch in progress: send it, then
            # let the tool finish.
            steering = asyncio.ensure_future(
                run.steer("New instruction: end your answer with the word PLUM-91.")
            )
            await asyncio.sleep(0.5)
            assert not steering.done(), "the steer answered before the tool batch ended"
            release.set()
            steer_id = await steering
            await pump
        result = await run.result()

    alignments = [
        e.event
        for e in envelopes
        if isinstance(e.event, UserEvent) and e.event.subtype == "alignment"
    ]
    assert [a.steer_id for a in alignments] == [steer_id]
    assert "PLUM-91" in message_text(alignments[0].message)
    assert "PLUM-91" in (result.final_text or "")  # type: ignore[union-attr]
