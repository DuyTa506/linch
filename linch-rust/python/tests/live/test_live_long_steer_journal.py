"""Live e2e (area long): what a cancel does to the steers that wait (S7 follow-up)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from e2e_long_support import dump
from live_core_support import ONLY_VLLM, Live, live_server, make_config, needs_pg
from live_sched_support import pg_dsn, query

from linch_rust import ToolCallStartEvent, UserEvent, tool

__all__ = ["pg_dsn"]

pytestmark = pytest.mark.live

LIVE: Live = ONLY_VLLM[0].values[0]  # type: ignore[assignment]
PROMPT = "Call the slow_task tool. When it returns, follow any new instruction, then answer in one short sentence."


@needs_pg
async def test_waiting_steers_answered_by_a_cancel_are_in_the_journal_and_never_delivered(
    pg_dsn: str,
) -> None:
    """The 16 waiting calls were answered with their ids when the run was cancelled: are they
    durable (`steer_received`), and is any of them delivered?"""
    started = asyncio.Event()
    release = asyncio.Event()

    @tool(scope="read")
    async def slow_task() -> str:
        """A long task."""
        started.set()
        await release.wait()
        return "task finished"

    config = make_config(LIVE, LIVE.spec(max_turns=5), stores={"type": "postgres", "dsn": pg_dsn})
    out: dict[str, Any] = {}

    async def pump(run: Any) -> None:
        async for envelope in run:
            if isinstance(envelope.event, ToolCallStartEvent):
                started.set()

    async with live_server() as server:
        agent = await server.open_agent(config, tools=[slow_task])
        run = await agent.run("s1", PROMPT)
        stream = asyncio.create_task(pump(run))
        async with asyncio.timeout(180):
            await started.wait()
            waiting = [
                asyncio.ensure_future(agent.steer(run.id, f"note {i}", steer_id=f"w-{i}"))
                for i in range(16)
            ]
            await asyncio.sleep(1.0)
            await run.cancel()
            outcomes = await asyncio.gather(*waiting, return_exceptions=True)
            out["answers"] = [o if isinstance(o, str) else type(o).__name__ for o in outcomes]
            release.set()
            await stream
        events = [e.event for e in await agent.events(run.id)]
    out["alignments_delivered"] = sum(
        isinstance(e, UserEvent) and e.subtype == "alignment" for e in events
    )
    rows = query(
        pg_dsn,
        "SELECT count(*) FROM linch_run_events WHERE envelope LIKE %s",
        '%"type":"steer_received"%',
    )
    out["steer_received_rows"] = int(rows[0][0])
    dump("steer_cancel_journal", out)
    answered = sum(isinstance(a, str) for a in out["answers"])
    print(
        f"steers answered {answered}/16, journaled {out['steer_received_rows']}, "
        f"delivered {out['alignments_delivered']}"
    )
