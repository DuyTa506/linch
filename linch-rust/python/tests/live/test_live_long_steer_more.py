"""Live e2e (area long): steer edge cases: the 16 KiB bound is in bytes, and a run another server drives."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from e2e_long_support import dump
from live_core_support import ONLY_VLLM, Live, live_server, make_config, needs_pg
from live_sched_support import pg_dsn

from linch_rust import InvalidSteer, RunLeased, ToolCallStartEvent, tool

__all__ = ["pg_dsn"]

pytestmark = pytest.mark.live

LIVE: Live = ONLY_VLLM[0].values[0]  # type: ignore[assignment]
PROMPT = "Call the slow_task tool. When it returns, answer in one short sentence."


async def test_the_steer_size_bound_counts_bytes_not_characters() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    @tool(scope="read")
    async def slow_task() -> str:
        """A long task."""
        started.set()
        await release.wait()
        return "task finished"

    out: dict[str, Any] = {}

    async def pump(run: Any) -> None:
        async for envelope in run:
            if isinstance(envelope.event, ToolCallStartEvent):
                started.set()

    async with live_server() as server:
        agent = await server.open_agent(make_config(LIVE, LIVE.spec(max_turns=5)), tools=[slow_task])
        run = await agent.run("s1", PROMPT)
        stream = asyncio.create_task(pump(run))
        async with asyncio.timeout(120):
            await started.wait()
            # 8193 two-byte characters are 16386 bytes: refused although far under 16384 characters.
            with pytest.raises(InvalidSteer) as refused:
                await agent.steer(run.id, "é" * 8193)
            out["8193_two_byte_chars"] = {"code": refused.value.code, "reason": getattr(refused.value, "reason", None)}
            accepted = asyncio.ensure_future(agent.steer(run.id, "é" * 8192, steer_id="bytes-ok"))
            await asyncio.sleep(0.5)
            release.set()
            out["8192_two_byte_chars"] = await accepted
            await stream
    dump("steer_bytes_bound", out)
    assert out["8193_two_byte_chars"]["code"] == -32038
    assert out["8192_two_byte_chars"] == "bytes-ok"


@needs_pg
async def test_a_steer_through_another_server_of_a_running_run_is_leased(pg_dsn: str) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    @tool(scope="read")
    async def slow_task() -> str:
        """A long task."""
        started.set()
        await release.wait()
        return "task finished"

    config = make_config(
        LIVE, LIVE.spec(max_turns=5), stores={"type": "postgres", "dsn": pg_dsn, "lease_ttl_ms": 5000}
    )
    out: dict[str, Any] = {}

    async def pump(run: Any) -> None:
        async for envelope in run:
            if isinstance(envelope.event, ToolCallStartEvent):
                started.set()

    async with live_server() as one, live_server() as two:
        agent1 = await one.open_agent(config, tools=[slow_task])
        agent2 = await two.open_agent(config, tools=[slow_task])
        run = await agent1.run("s1", PROMPT)
        stream = asyncio.create_task(pump(run))
        async with asyncio.timeout(120):
            await started.wait()
            with pytest.raises(RunLeased) as leased:
                await agent2.steer(run.id, "from the other server", steer_id="other-1")
            out["leased"] = {"code": leased.value.code, "expires_in": leased.value.expires_in}
            release.set()
            await stream
    dump("steer_other_server", out)
    assert out["leased"]["code"] == -32033
