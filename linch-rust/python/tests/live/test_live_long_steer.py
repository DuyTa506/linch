"""Live e2e (area long): steering (expectations S1-S11, M1 of `long_results/expectations.md`).

A real vLLM model through the sidecar; the restart cases use Postgres stores
and `kill -9`. Raw data goes to `$E2E_RESULTS/steer_*.json`.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from typing import Any

import pytest
from e2e_long_support import dump, load
from live_core_support import (
    ONLY_VLLM,
    Live,
    live_server,
    make_config,
    message_text,
    needs_pg,
)
from live_sched_support import pg_dsn, query

from linch_rust import (
    Allow,
    AssistantEvent,
    ConnectionClosed,
    InvalidSteer,
    LimitReached,
    PermissionRequestEvent,
    ResultEvent,
    RunError,
    RunLeased,
    RunNotFound,
    RunTerminal,
    SuspendedEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    UserEvent,
    tool,
)

__all__ = ["pg_dsn"]

pytestmark = pytest.mark.live

LIVE: Live = ONLY_VLLM[0].values[0]  # type: ignore[assignment]
PROMPT = (
    "Call the slow_task tool. When it returns, follow any new instruction you received while "
    "it ran, then answer in one short sentence."
)


def now() -> float:
    return time.time()


async def pump(run: Any, into: list[Any], started: asyncio.Event | None = None) -> None:
    async for envelope in run:
        into.append((now(), envelope))
        if started is not None and isinstance(envelope.event, ToolCallStartEvent):
            started.set()


def alignments(envelopes: list[Any]) -> list[Any]:
    return [
        (t, e)
        for t, e in envelopes
        if isinstance(e.event, UserEvent) and e.event.subtype == "alignment"
    ]


def held_tool(seconds: float, started: asyncio.Event) -> Any:
    @tool(scope="read")
    async def slow_task() -> str:
        """A long task."""
        started.set()
        await asyncio.sleep(seconds)
        return "task finished"

    return slow_task


async def test_steer_during_a_tool_call_queues_then_aligns_and_is_followed() -> None:
    """S1 S2 S3 S11 M1: five steers, one per run, signed latencies recorded."""
    rows: list[dict[str, Any]] = []
    out: dict[str, Any] = {"load": load()}
    for n in range(5):
        started = asyncio.Event()
        slow = held_tool(4, started)
        word = f"PLUM-{90 + n}"
        async with live_server() as server:
            agent = await server.open_agent(
                make_config(LIVE, LIVE.spec(max_turns=5)), tools=[slow]
            )
            run = await agent.run("s1", PROMPT)
            envelopes: list[Any] = []
            task = asyncio.create_task(pump(run, envelopes, started))
            async with asyncio.timeout(180):
                await started.wait()
                await asyncio.sleep(1.0)  # into the tool batch
                params = {
                    "agent": agent.handle,
                    "run_id": run.id,
                    "text": f"New instruction: end your answer with the word {word}.",
                    "id": f"steer-{n}",
                }
                t_call = now()
                first = asyncio.ensure_future(agent._conn.request("run.steer", params))
                # S11: the same id sent again while the first still waits.
                again = asyncio.ensure_future(agent._conn.request("run.steer", params))
                await asyncio.sleep(1.0)
                still_waiting = not first.done()
                raw = await first
                t_return = now()
                raw2 = await again
                await task
            result = await run.result()
        aligned = alignments(envelopes)
        end = next(e for _, e in envelopes if isinstance(e.event, ToolCallEndEvent))
        asst_after = [
            e for _, e in envelopes if isinstance(e.event, AssistantEvent) and e.seq > end.seq
        ]
        row = {
            "n": n,
            "raw_answer": raw,
            "raw_answer_again": raw2,
            "still_waiting_after_2s_of_a_4s_tool": still_waiting,
            "alignments": len(aligned),
            "alignment_steer_ids": [e.event.steer_id for _, e in aligned],
            "alignment_text": message_text(aligned[0][1].event.message) if aligned else None,
            "alignment_after_tool_end": bool(aligned and aligned[0][1].seq > end.seq),
            "assistant_after_tool_end": len(asst_after),
            "t_call_to_return_s": round(t_return - t_call, 3),
            "t_return_to_event_s": round(aligned[0][0] - t_return, 3) if aligned else None,
            "t_call_to_event_s": round(aligned[0][0] - t_call, 3) if aligned else None,
            "server_ts_event_minus_tool_end_ms": (
                aligned[0][1].ts_ms - end.ts_ms if aligned else None
            ),
            "final_text": getattr(result, "final_text", None),
            "word_followed": word in (getattr(result, "final_text", "") or ""),
        }
        rows.append(row)
    out["rows"] = rows
    dump("steer_semantics_m1", out)
    for row in rows:
        assert row["raw_answer"] == {"queued": True, "id": f"steer-{row['n']}"}, row
        assert row["raw_answer_again"]["id"] == row["raw_answer"]["id"]
        assert row["still_waiting_after_2s_of_a_4s_tool"], row
        assert row["alignments"] == 1, row  # S11: one alignment for the two calls
        assert row["alignment_after_tool_end"], row
    followed = sum(r["word_followed"] for r in rows)
    print(f"steer followed by the model: {followed}/5")
    assert followed >= 4, rows


async def test_steer_refusals() -> None:
    """S4 S5: finished/unknown runs, blank, too long, bad id; 16 KiB exactly is taken."""
    started = asyncio.Event()
    slow = held_tool(5, started)
    out: dict[str, Any] = {}
    async with live_server() as server:
        agent = await server.open_agent(make_config(LIVE, LIVE.spec(max_turns=5)), tools=[slow])
        run = await agent.run("s1", PROMPT)
        envelopes: list[Any] = []
        task = asyncio.create_task(pump(run, envelopes, started))
        async with asyncio.timeout(180):
            await started.wait()
            for label, text, steer_id, exc in [
                ("blank", "   ", None, InvalidSteer),
                ("16385_bytes", "x" * 16385, None, InvalidSteer),
                ("bad_id", "hello", "no spaces allowed!", InvalidSteer),
                ("id_129_bytes", "hello", "a" * 129, InvalidSteer),
            ]:
                params: dict[str, Any] = {"agent": agent.handle, "run_id": run.id, "text": text}
                if steer_id:
                    params["id"] = steer_id
                t0 = now()
                with pytest.raises(exc) as raised:
                    await agent._conn.request("run.steer", params)
                out[label] = {
                    "code": raised.value.code,
                    "reason": getattr(raised.value, "reason", None),
                    "answered_in_s": round(now() - t0, 3),
                }
            exact = await agent.steer(run.id, "y" * 16384 + "", steer_id="exact-16k")
            out["16384_bytes"] = exact
            await task
        await run.result()
        aligned = alignments(envelopes)
        out["16384_alignment_chars"] = len(message_text(aligned[0][1].event.message))
        with pytest.raises(RunTerminal) as late:
            await agent.steer(run.id, "too late")
        out["finished_run"] = {"code": late.value.code, "data": late.value.data}
        with pytest.raises(RunNotFound) as missing:
            await agent.steer("no-such-run", "hello")
        out["unknown_run"] = {"code": missing.value.code}
    dump("steer_refusals", out)
    assert out["blank"]["code"] == -32038 and out["16385_bytes"]["code"] == -32038
    assert out["bad_id"]["code"] == -32038 and out["id_129_bytes"]["code"] == -32038
    assert all(out[k]["answered_in_s"] < 1 for k in ("blank", "16385_bytes", "bad_id"))
    assert out["16384_bytes"] == "exact-16k"
    assert out["finished_run"]["code"] == -32026 and out["unknown_run"]["code"] == -32021


async def test_the_17th_waiting_steer_is_refused_and_cancel_is_not_blocked() -> None:
    """S6 S7."""
    started = asyncio.Event()
    release = asyncio.Event()

    @tool(scope="read")
    async def slow_task() -> str:
        """A long task."""
        started.set()
        await release.wait()
        return "task finished"

    out: dict[str, Any] = {}
    async with live_server() as server:
        agent = await server.open_agent(
            make_config(LIVE, LIVE.spec(max_turns=5)), tools=[slow_task]
        )
        run = await agent.run("s1", PROMPT)
        envelopes: list[Any] = []
        stream = asyncio.create_task(pump(run, envelopes, started))
        async with asyncio.timeout(180):
            await started.wait()
            waiting = [
                asyncio.ensure_future(agent.steer(run.id, f"note {i}", steer_id=f"w-{i}"))
                for i in range(16)
            ]
            await asyncio.sleep(1.5)
            out["first_16_done_while_tool_runs"] = sum(w.done() for w in waiting)
            t0 = now()
            with pytest.raises(LimitReached) as refused:
                await agent.steer(run.id, "the 17th", steer_id="w-16")
            out["17th"] = {
                "code": refused.value.code,
                "data": refused.value.data,
                "answered_in_s": round(now() - t0, 3),
            }
            t0 = now()
            cancelled = await asyncio.wait_for(run.cancel(), 10)
            out["cancel"] = {"returned": cancelled, "answered_in_s": round(now() - t0, 3)}
            outcomes = await asyncio.gather(*waiting, return_exceptions=True)
            out["waiting_after_cancel"] = [
                o if isinstance(o, str) else f"{type(o).__name__}:{getattr(o, 'code', '')}"
                for o in outcomes
            ]
            release.set()
            await stream
        final = await run.result()
        out["final_subtype"] = getattr(final, "subtype", None)
    dump("steer_limit_cancel", out)
    assert out["first_16_done_while_tool_runs"] == 0
    assert out["17th"]["code"] == -32005 and out["17th"]["data"]["kind"] == "steers"
    assert out["17th"]["answered_in_s"] < 1
    assert out["cancel"]["returned"] is True and out["cancel"]["answered_in_s"] < 2
    assert out["final_subtype"] == "aborted"


def steer_rows(dsn: str, steer_id: str) -> int:
    rows = query(
        dsn,
        "SELECT count(*) FROM linch_run_events WHERE envelope LIKE %s AND envelope LIKE %s",
        '%"type":"steer_received"%',
        f'%"{steer_id}"%',
    )
    return int(rows[0][0])


@needs_pg
async def test_a_steer_of_a_suspended_run_survives_kill_9_and_is_delivered_once(
    pg_dsn: str,
) -> None:
    """S8 S9: steer a run awaiting approval, kill -9 the server, restart, approve."""

    @tool(scope="write")
    async def deploy(service: str) -> str:
        """Deploy a service."""
        return f"deployed {service}"

    config = make_config(
        LIVE,
        LIVE.spec(max_turns=6),
        stores={"type": "postgres", "dsn": pg_dsn, "lease_ttl_ms": 1000},
    )
    out: dict[str, Any] = {}
    async with live_server() as server:
        agent = await server.open_agent(config, tools=[deploy])
        run = await agent.run("s1", "Call the deploy tool for service api, then answer in one short sentence.")
        call_ids: list[str] = []
        async with asyncio.timeout(120):
            async for envelope in run:
                if isinstance(envelope.event, PermissionRequestEvent):
                    call_ids += [r["tool_use_id"] for r in envelope.event.requests]
                suspended = envelope.event
        assert isinstance(suspended, SuspendedEvent) and call_ids
        t0 = now()
        raw = await agent._conn.request(
            "run.steer",
            {
                "agent": agent.handle,
                "run_id": run.id,
                "text": "New instruction: end your answer with the word FIG-55.",
                "id": "sus-1",
            },
        )
        out["steer_of_suspended"] = {"answer": raw, "answered_in_s": round(now() - t0, 3)}
        out["steer_rows_before_kill"] = steer_rows(pg_dsn, "sus-1")
        # still suspended: a second run on the session is busy; the run record says so
        out["status_after_steer"] = query(
            pg_dsn, "SELECT status FROM linch_runs WHERE run_id = %s", run.id
        )
        os.kill(server.pid, signal.SIGKILL)

    async with live_server() as server:
        agent = await server.open_agent(config, tools=[deploy])
        approved = None
        for _ in range(30):
            approved = await agent.approve(run.id, {call_ids[0]: Allow()})
            try:
                async with asyncio.timeout(120):
                    result = await approved.result()
                break
            except RunLeased as leased:
                await asyncio.sleep(leased.expires_in or 0.5)
        events = await agent.events(run.id)
    aligned = [
        e.event
        for e in events
        if isinstance(e.event, UserEvent) and e.event.subtype == "alignment"
    ]
    out["alignments_after_approve"] = [(a.steer_id, message_text(a.message)) for a in aligned]
    out["steer_received_rows"] = steer_rows(pg_dsn, "sus-1")
    out["final_text"] = getattr(result, "final_text", None)
    dump("steer_suspended_restart", out)
    assert out["steer_of_suspended"]["answer"] == {"queued": True, "id": "sus-1"}
    assert out["steer_of_suspended"]["answered_in_s"] < 2
    assert [a[0] for a in out["alignments_after_approve"]] == ["sus-1"]
    assert out["steer_received_rows"] == 1


@needs_pg
async def test_a_delivered_steer_is_not_logged_again_after_kill_9_and_a_dead_runs_steer(
    pg_dsn: str,
) -> None:
    """S9 (running) and S10: kill after the alignment, resume; steer a run nobody drives."""
    started = asyncio.Event()
    slow = held_tool(3, started)
    config = make_config(
        LIVE,
        LIVE.spec(max_turns=6),
        stores={"type": "postgres", "dsn": pg_dsn, "lease_ttl_ms": 1000},
    )
    out: dict[str, Any] = {}
    async with live_server() as server:
        agent = await server.open_agent(config, tools=[slow])
        run = await agent.run("s1", PROMPT)
        envelopes: list[Any] = []
        task = asyncio.create_task(pump(run, envelopes, started))
        async with asyncio.timeout(120):
            await started.wait()
            steer_id = await agent.steer(run.id, "New instruction: end with the word KIWI-12.", steer_id="run-1")
            while not alignments(envelopes):
                await asyncio.sleep(0.02)
        os.kill(server.pid, signal.SIGKILL)
        with contextlib.suppress(ConnectionClosed):
            await task
        assert steer_id == "run-1"

    async with live_server() as server:
        agent = await server.open_agent(config, tools=[slow])
        # S10: the run is `running` and nobody drives it; its crashed lease is still alive.
        try:
            await agent.steer(run.id, "anyone there?", steer_id="dead-1")
            out["steer_of_undriven_run"] = "accepted"
        except (RunLeased, InvalidSteer, RunError) as refused:
            out["steer_of_undriven_run"] = {
                "type": type(refused).__name__,
                "code": getattr(refused, "code", None),
                "expires_in": getattr(refused, "expires_in", None),
            }
        await asyncio.sleep(2.0)
        try:
            await agent.steer(run.id, "anyone there now?", steer_id="dead-2")
            out["steer_after_lease_expiry"] = "accepted"
        except (RunLeased, InvalidSteer, RunError) as refused:
            out["steer_after_lease_expiry"] = {
                "type": type(refused).__name__,
                "code": getattr(refused, "code", None),
            }
        result = None
        for _ in range(30):
            resumed = await agent.resume(run.id)
            try:
                async with asyncio.timeout(120):
                    result = await resumed.result()
                break
            except RunLeased as leased:
                await asyncio.sleep(leased.expires_in or 0.5)
        events = await agent.events(run.id)
    aligned = [
        e.event
        for e in events
        if isinstance(e.event, UserEvent) and e.event.subtype == "alignment"
    ]
    out["alignment_ids_after_resume"] = [a.steer_id for a in aligned]
    out["steer_received_rows"] = steer_rows(pg_dsn, "run-1")
    out["result"] = getattr(result, "subtype", None)
    dump("steer_running_restart", out)
    assert isinstance(result, ResultEvent)
    assert out["alignment_ids_after_resume"] == ["run-1"], out
    assert out["steer_received_rows"] == 1
