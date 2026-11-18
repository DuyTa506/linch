"""Live: several workers on one database. A worker killed in the middle of a
real model call hands its job to another, which finishes the run; two
workers fire each schedule tick once. Skips without the live environment.
"""

from __future__ import annotations

import asyncio

import pytest
from live_sched_support import (
    LiveServer,
    kind,
    live_config,
    pg_dsn,
    query,
    seqs_increasing,
    until,
    vllm,
)

from linch_rust import ResultEvent, UsageEvent

__all__ = ["kind", "pg_dsn", "vllm"]

pytestmark = pytest.mark.live

LONG = "Write the integers from 1 to 600, one per line, and nothing else."


async def test_a_job_whose_worker_was_killed_is_finished_by_another(vllm: str, pg_dsn: str) -> None:
    # A short visibility timeout and run lease (2 s) so the takeover is quick.
    worker = {"visibility_timeout_ms": 3000, "poll_interval_ms": 250}
    config = live_config(vllm, "acme/takeover", pg_dsn, spec={"max_output_tokens": 1500})
    one = LiveServer(pg_dsn, worker={**worker, "owner": "worker-one"})
    two = LiveServer(pg_dsn, worker={**worker, "owner": "worker-two"})
    await one.start()
    try:
        async with one.connect() as server:
            agent = await server.open_agent(config)
            job = await agent.enqueue("long", LONG)
            await until(
                lambda: _state(agent, job["job_id"], "running"),
                timeout=30,
                every=0.2,
                what="the job to run",
            )
            await asyncio.sleep(1.5)  # inside the model call
        await one.kill()
        assert query(pg_dsn, "SELECT count(*) FROM linch_worker_jobs")[0][0] == 1, "still queued"

        await two.start()
        async with two.connect() as server:
            agent = await server.open_agent(config)
            done = await agent.wait_job(job["job_id"], timeout=120)
            assert done["state"] == "done", done
            assert done["attempts"] >= 2, "a second attempt, on the other worker"
            run_id = done.get("run_id")
            assert run_id is not None
            events = await agent.events(run_id)
        assert seqs_increasing(events)
        result = events[-1].event
        assert isinstance(result, ResultEvent) and result.subtype == "success", result
        assert len([e for e in events if isinstance(e.event, ResultEvent)]) == 1
        usage = [e.event for e in events if isinstance(e.event, UsageEvent)]
        assert len(usage) == 1, "the killed call was never paid; the retry once"
        assert result.total_usage["output_tokens"] == usage[0].usage["output_tokens"]
        rows = query(pg_dsn, "SELECT outcome, attempts FROM linch_worker_history")
        assert [r[0] for r in rows] == ["done"]
    finally:
        await one.kill()
        await two.stop()
        one.cleanup()
        two.cleanup()


async def _state(agent: object, job_id: int, state: str) -> bool:
    return (await agent.job(job_id))["state"] == state  # type: ignore[attr-defined]


async def test_two_workers_fire_every_tick_once(kind: str, pg_dsn: str) -> None:
    worker = {"poll_interval_ms": 100}
    one = LiveServer(pg_dsn, worker={**worker, "owner": "worker-one"})
    two = LiveServer(pg_dsn, worker={**worker, "owner": "worker-two"})
    await one.start()
    await two.start()
    try:
        async with one.connect() as server:
            agent = await server.open_agent(live_config(kind, "acme/twice", pg_dsn))
            await agent.create_schedule(
                "Reply with the single word PONG.", interval_ms=3000, id="tick"
            )

            async def enough() -> bool:
                jobs = await agent.jobs(schedule_id="tick")
                return sum(j["state"] == "done" for j in jobs) >= 4

            await until(enough, timeout=120, every=1, what="four fires")
            await agent.pause_schedule("tick")
            await asyncio.sleep(2)
            jobs = await agent.jobs(schedule_id="tick")
        fire_times = [j.get("fire_at") for j in jobs]
        assert len(set(fire_times)) == len(fire_times), "no tick fired twice"
        runs = [j.get("run_id") for j in jobs if j.get("run_id")]
        assert len(set(runs)) == len(runs), "no run started twice"
        [(history,)] = query(
            pg_dsn, "SELECT count(*) FROM linch_worker_history WHERE schedule_id = 'tick'"
        )
        assert history == sum(j["state"] == "done" for j in jobs)
    finally:
        await one.stop()
        await two.stop()
        one.cleanup()
        two.cleanup()
