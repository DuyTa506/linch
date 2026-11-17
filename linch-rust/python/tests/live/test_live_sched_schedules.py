"""Live: interval and cron schedules, heartbeats, jobs and pause, resume,
delete, through a real `linch-server --config` worker, Postgres and a real
model. Skips without the live environment (see `live_sched_support`).

Assertions are on durable facts (job rows, `linch_worker_history`, run
events), never on the model's wording. A model turn is not retried.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from live_sched_support import (
    ended,
    final_text,
    kind,
    live_config,
    pg_dsn,
    query,
    seqs_increasing,
    serving,
    until,
    vllm,
)

from linch_rust import Agent, InitEvent, Job, ResultEvent

__all__ = ["kind", "pg_dsn", "vllm"]

pytestmark = pytest.mark.live

PING = "Reply with the single word PONG and nothing else."


async def done_jobs(agent: Agent, schedule_id: str, count: int) -> list[Job]:
    async def check() -> list[Job] | None:
        jobs = await agent.jobs(schedule_id=schedule_id)
        finished = ended(jobs)
        return finished if len(finished) >= count else None

    return await until(check, timeout=150, every=1, what=f"{count} finished fires of {schedule_id}")


async def test_a_heartbeat_fires_on_an_interval_and_pauses_resumes_and_deletes(
    kind: str, pg_dsn: str
) -> None:
    async with serving(pg_dsn) as live, live.connect() as server:
        agent = await server.open_agent(live_config(kind, "acme", pg_dsn))
        beat = await agent.heartbeat("beat", interval_ms=5000, prompt=PING, id="beat")
        assert beat["id"] == "beat" and beat.get("session_id") == "beat"

        finished = await done_jobs(agent, "beat", 2)
        assert {job.get("session_id") for job in finished} == {"beat"}
        run_ids = [job.get("run_id") for job in finished]
        assert None not in run_ids and len(set(run_ids)) == len(run_ids), "a new run per fire"
        for job in finished:
            run_id = job.get("run_id")
            assert run_id is not None
            events = await agent.events(run_id)
            assert seqs_increasing(events)
            assert isinstance(events[0].event, InitEvent)
            last = events[-1].event
            assert isinstance(last, ResultEvent) and last.subtype == "success"
            assert last.usage_complete
            assert final_text(events).strip(), "the model answered"

        [listed] = await agent.schedules()
        assert listed["fires"] >= 2 and listed.get("interval_ms") == 5000
        assert listed.get("last_fire")

        paused = await agent.pause_schedule("beat")
        assert paused["paused"]
        # A fire already queued may still run; none is made after the pause.
        await asyncio.sleep(1)
        fires = (await agent.schedules())[0]["fires"]
        await asyncio.sleep(8)
        [still] = await agent.schedules()
        assert still["fires"] == fires and still["paused"]

        resumed = await agent.resume_schedule("beat")
        assert not resumed["paused"]
        await until(
            lambda: _fires_above(agent, fires), timeout=40, every=1, what="a fire after resume"
        )
        assert await agent.delete_schedule("beat")
        gone = len(await agent.jobs(schedule_id="beat"))
        await asyncio.sleep(7)
        assert await agent.schedules() == []
        assert len(await agent.jobs(schedule_id="beat")) == gone, "no fire after the delete"

    # The history table holds every ended fire's job.
    rows = query(
        pg_dsn,
        "SELECT outcome, schedule_id FROM linch_worker_history WHERE schedule_id = 'beat'",
    )
    assert len(rows) >= 3 and {row[1] for row in rows} == {"beat"}
    assert {row[0] for row in rows} <= {"done"}


async def _fires_above(agent: Agent, count: int) -> bool:
    [schedule] = await agent.schedules()
    return schedule["fires"] > count


async def test_a_cron_schedule_fires_at_the_minute(vllm: str, pg_dsn: str) -> None:
    # `* * * * *` fires at the next minute boundary: up to 60 s of real waiting.
    async with serving(pg_dsn) as live, live.connect() as server:
        agent = await server.open_agent(live_config(vllm, "acme", pg_dsn))
        started = time.time()
        created = await agent.create_schedule(PING, cron="* * * * *", id="cron")
        assert created.get("cron") == "* * * * *"
        [job] = (await done_jobs(agent, "cron", 1))[:1]
        assert time.time() - started < 130
        assert job.get("session_id", "").startswith("schedule:cron:"), "a session per fire"
        run_id = job.get("run_id")
        assert run_id is not None
        events = await agent.events(run_id)
        assert isinstance(events[-1].event, ResultEvent) and events[-1].event.subtype == "success"
        [listed] = await agent.schedules()
        assert listed["fires"] >= 1
        assert await agent.delete_schedule("cron")


async def test_missed_fires_coalesce_after_the_workers_were_down(vllm: str, pg_dsn: str) -> None:
    async with serving(pg_dsn) as live:
        async with live.connect() as server:
            agent = await server.open_agent(live_config(vllm, "acme", pg_dsn))
            await agent.create_schedule(PING, interval_ms=2000, id="tick")
            await done_jobs(agent, "tick", 1)
        await live.stop()
        # Read after the stop: a fire may land while the server shuts down.
        [(fires, skips)] = query(
            pg_dsn, "SELECT fires, skips FROM linch_worker_schedules WHERE id = 'tick'"
        )
        before = {"fires": fires, "skips": skips}
        await asyncio.sleep(9)  # four or more ticks pass with no worker
        await live.start()
        async with live.connect() as server:
            agent = await server.open_agent(live_config(vllm, "acme", pg_dsn))
            after = await until(
                lambda: _fired_since(agent, before["fires"]),
                timeout=60,
                every=0.5,
                what="the first fire after the restart",
            )
            assert after["skips"] >= before["skips"] + 3, (before, after)
            assert after["fires"] == before["fires"] + 1, "one fire for all the missed ticks"
            assert await agent.delete_schedule("tick")


async def _fired_since(agent: Agent, fires: int) -> object:
    [schedule] = await agent.schedules()
    return schedule if schedule["fires"] > fires else None


async def test_a_job_runs_on_a_worker_and_a_failing_job_fails(kind: str, pg_dsn: str) -> None:
    async with serving(pg_dsn) as live, live.connect() as server:
        agent = await server.open_agent(live_config(kind, "acme", pg_dsn))
        job = await agent.enqueue("jobs-1", PING)
        assert job["state"] in ("queued", "running", "done")
        done = await agent.wait_job(job["job_id"], timeout=120)
        assert done["state"] == "done" and done.get("session_id") == "jobs-1"
        run_id = done.get("run_id")
        assert run_id is not None
        events = await agent.events(run_id)
        assert final_text(events).strip()

    rows = query(
        pg_dsn, "SELECT outcome, attempts FROM linch_worker_history WHERE session_id = 'jobs-1'"
    )
    assert rows == [("done", 1)]
