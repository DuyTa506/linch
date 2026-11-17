"""Schedules and background runs through a `linch-server --config` with a
`worker` section: a queued run and a heartbeat run in the background, and
the scheduler's refusals arrive as typed errors.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import signal
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from scripted import MODEL, text_turn, tool_turn
from test_resume import pg_dsn

from linch_rust import (
    CHECK_IN,
    AgentConfig,
    InitEvent,
    InvalidSchedule,
    Job,
    JobNotFound,
    LimitReached,
    NotAllowed,
    NotBackground,
    Principal,
    ResultEvent,
    ScheduleExists,
    ScheduleNotFound,
    Server,
    ToolCallEndEvent,
    find_server,
    tool,
)

__all__ = ["pg_dsn"]

TOKEN = "tok-acme-scheduler-test-0001"


@asynccontextmanager
async def serving(dsn: str, *, allow: dict[str, Any], worker: bool = True) -> AsyncIterator[str]:
    """Run `linch-server --config` with tenant `acme` and, when `worker` is
    set, a worker on `dsn`; yield its socket address."""
    directory = Path(tempfile.mkdtemp(prefix="ls-", dir="/tmp"))
    allowed = {"scripted_provider": True, "postgres_dsns": [dsn], **allow}
    config: dict[str, Any] = {
        "socket": str(directory / "linch.sock"),
        "tenants": {"acme": {"tokens": [{"value": TOKEN}], "allow": allowed}},
    }
    if worker:
        config["worker"] = {"dsn": {"value": dsn}, "poll_interval_ms": 50, "retry_delay_ms": 100}
    path = directory / "server.json"
    path.write_text(json.dumps(config))
    process = await asyncio.create_subprocess_exec(
        find_server(), "--config", str(path), stderr=asyncio.subprocess.PIPE, cwd=directory
    )
    assert process.stderr is not None
    try:
        while True:
            line = await asyncio.wait_for(process.stderr.readline(), 30)
            assert line, "the server exited before it listened"
            _, _, address = line.decode().strip().partition("listening on ")
            if address:
                break
        yield address
    finally:
        if process.returncode is None:
            process.send_signal(signal.SIGTERM)
        await asyncio.wait_for(process.wait(), 15)
        shutil.rmtree(directory, ignore_errors=True)


def acme(dsn: str | None, **extra: Any) -> AgentConfig:
    """A scripted config in tenant `acme`'s namespace, on Postgres when `dsn`."""
    fields: dict[str, Any] = {
        "namespace": "acme",
        "spec": {"model": MODEL, "max_turns": 100},
        "provider": {"id": "scripted", "turns": [text_turn("Checked in.")]},
        **extra,
    }
    if dsn is not None:
        fields["stores"] = {"type": "postgres", "dsn": dsn}
    return cast(AgentConfig, fields)


async def test_a_queued_run_and_a_heartbeat_run_in_the_background(pg_dsn: str) -> None:
    allow = {"max_schedules": 2, "max_queued_jobs": 8, "min_schedule_interval_ms": 100}
    async with serving(pg_dsn, allow=allow) as at, Server.connect(at, token=TOKEN) as server:
        agent = await server.open_agent(acme(pg_dsn))
        queued = await agent.enqueue("s1", "Go.")
        assert queued["state"] in ("queued", "running", "done")
        done = await agent.wait_job(queued["job_id"], timeout=30)
        assert done["state"] == "done" and done.get("session_id") == "s1"
        run_id = done.get("run_id")
        assert run_id is not None
        events = await agent.events(run_id)
        last = events[-1].event
        assert isinstance(last, ResultEvent) and last.final_text == "Checked in."

        beat = await agent.heartbeat("ops", interval_ms=200, id="beat")
        assert (beat["id"], beat.get("session_id"), beat["prompt"]) == ("beat", "ops", CHECK_IN)
        fired: list[Job] = []
        for _ in range(200):
            fired = await agent.jobs(schedule_id="beat")
            if sum(job["state"] == "done" for job in fired) >= 2:
                break
            await asyncio.sleep(0.05)
        ended = [job for job in fired if job["state"] == "done"]
        assert len(ended) >= 2
        assert {job.get("session_id") for job in ended} == {"ops"}
        assert len({job.get("run_id") for job in ended}) == len(ended), "a new run per fire"
        [listed] = await agent.schedules()
        assert listed["fires"] >= 2 and listed.get("interval_ms") == 200
        paused = await agent.pause_schedule("beat")
        assert paused["paused"]
        resumed = await agent.resume_schedule("beat")
        assert not resumed["paused"]
        assert await agent.delete_schedule("beat")
        assert not await agent.delete_schedule("beat")


async def test_the_scheduler_refusals_are_typed(pg_dsn: str) -> None:
    @tool(scope="read")
    async def lookup(q: str) -> str:
        """Look something up."""
        return q

    allow = {"max_schedules": 2, "max_queued_jobs": 1}
    async with serving(pg_dsn, allow=allow, worker=False) as at:
        async with Server.connect(at, token=TOKEN) as server:
            memory = await server.open_agent(acme(None))
            with pytest.raises(NotBackground) as refused:
                await memory.create_schedule("p", interval_ms=60_000)
            assert refused.value.reason == "memory_store"
            hosted = await server.open_agent(
                acme(pg_dsn, provider={"id": "scripted", "turns": [tool_turn("c", "lookup", {})]}),
                tools=[lookup],
            )
            with pytest.raises(NotBackground) as refused:
                await hosted.enqueue("s", "p")
            assert refused.value.reason == "host_tools"

            agent = await server.open_agent(acme(pg_dsn))
            with pytest.raises(InvalidSchedule) as invalid:
                await agent.create_schedule("p", cron="0 9 * * *", timezone="Mars/Base")
            assert "Mars/Base" in invalid.value.reason
            with pytest.raises(InvalidSchedule):
                await agent.create_schedule("p")
            with pytest.raises(NotAllowed) as fast:
                await agent.create_schedule("p", interval_ms=1000)
            assert fast.value.kind == "schedule_interval"
            daily = await agent.create_schedule(
                "Digest.", cron="0 7 * * *", timezone="Europe/Berlin", id="digest"
            )
            assert daily.get("timezone") == "Europe/Berlin" and "session_id" not in daily
            with pytest.raises(ScheduleExists):
                await agent.create_schedule("p", cron="0 8 * * *", id="digest")
            # A principal of another tenant is refused, as for `run`.
            eve: Principal = {"tenant": "globex", "user": "eve", "roles": ["admin"]}
            with pytest.raises(NotAllowed) as foreign:
                await agent.create_schedule("p", cron="0 8 * * *", principal=eve)
            assert foreign.value.kind == "principal"
            with pytest.raises(NotAllowed) as foreign:
                await agent.enqueue("s", "p", principal=eve)
            assert foreign.value.kind == "principal"
            ada: Principal = {"tenant": "acme", "user": "ada", "roles": ["intern"]}
            await agent.create_schedule("p", cron="0 8 * * *", id="other", principal=ada)
            with pytest.raises(LimitReached) as capped:
                await agent.create_schedule("p", cron="0 9 * * *", id="third")
            assert (capped.value.kind, capped.value.max) == ("schedules", 2)
            with pytest.raises(ScheduleNotFound):
                await agent.pause_schedule("nope")
            with pytest.raises(JobNotFound):
                await agent.job(999_999)
            queued = await agent.enqueue("s", "p")
            assert queued["state"] == "queued"
            assert [job["job_id"] for job in await agent.jobs()] == [queued["job_id"]]
            with pytest.raises(LimitReached) as full:
                await agent.enqueue("s2", "p")
            assert full.value.kind == "jobs"


async def test_a_tenant_without_scheduler_allowlists_is_refused(pg_dsn: str) -> None:
    async with serving(pg_dsn, allow={}, worker=False) as at:
        async with Server.connect(at, token=TOKEN) as server:
            agent = await server.open_agent(acme(pg_dsn))
            with pytest.raises(NotAllowed) as refused:
                await agent.create_schedule("p", cron="0 * * * *")
            assert refused.value.kind == "schedules"
            with pytest.raises(NotAllowed) as refused:
                await agent.enqueue("s", "p")
            assert refused.value.kind == "background_runs"


SCHEDULE_TOOLS = {"CreateSchedule", "ListSchedules", "CancelSchedule"}


async def test_the_model_schedules_runs_of_its_own_agent(pg_dsn: str) -> None:
    allow = {
        "max_schedules": 1,
        "max_queued_jobs": 8,
        "min_schedule_interval_ms": 100,
        "permission_modes": ["default", "skip_dangerous"],
        "workspace_roots": ["/tmp"],
    }
    turns = [
        tool_turn("c1", "CreateSchedule", {"payload": "Check in.", "interval_s": 0.2}),
        text_turn("Scheduled."),
    ]
    on = acme(
        pg_dsn,
        provider={"id": "scripted", "turns": turns},
        permissions={"mode": "skip_dangerous", "project_root": "/tmp"},
        schedule_tools=True,
    )
    async with serving(pg_dsn, allow=allow) as at, Server.connect(at, token=TOKEN) as server:
        off = await server.open_agent(acme(pg_dsn))
        envelopes = [e async for e in await off.run("plain", "Hi.")]
        init = envelopes[0].event
        assert isinstance(init, InitEvent) and not SCHEDULE_TOOLS & set(init.tools)

        agent = await server.open_agent(on)
        ada: Principal = {"tenant": "acme", "user": "ada", "roles": ["ops"]}
        envelopes = [e async for e in await agent.run("chat", "Check in hourly.", principal=ada)]
        init = envelopes[0].event
        assert isinstance(init, InitEvent) and SCHEDULE_TOOLS <= set(init.tools)
        [made] = [e.event for e in envelopes if isinstance(e.event, ToolCallEndEvent)]
        assert made.error_code is None and made.metadata is not None
        [schedule] = await agent.schedules()
        assert schedule["id"] == made.metadata["id"]
        assert (schedule.get("session_id"), schedule.get("interval_ms")) == ("chat", 200)

        # A worker fires it: the same agent in the same session, where its
        # own CreateSchedule meets the tenant's cap.
        fired: list[Job] = []
        for _ in range(200):
            fired = await agent.jobs(schedule_id=schedule["id"])
            if any(job["state"] == "done" for job in fired):
                break
            await asyncio.sleep(0.05)
        assert await agent.delete_schedule(schedule["id"])
        done = next(job for job in fired if job["state"] == "done")
        assert done.get("session_id") == "chat"
        run_id = done.get("run_id")
        assert run_id is not None
        events = [envelope.event for envelope in await agent.events(run_id)]
        [capped] = [event for event in events if isinstance(event, ToolCallEndEvent)]
        assert capped.error_code == "limit_reached"
        last = events[-1]
        assert isinstance(last, ResultEvent) and last.final_text == "Scheduled."


async def test_schedule_tools_need_schedules_in_the_allowlists(pg_dsn: str) -> None:
    on = acme(pg_dsn, schedule_tools=True)
    async with serving(pg_dsn, allow={}, worker=False) as at:
        async with Server.connect(at, token=TOKEN) as server:
            with pytest.raises(NotAllowed) as refused:
                await server.open_agent(on)
            assert refused.value.kind == "schedule_tools"
    async with Server() as local:
        with pytest.raises(NotAllowed) as refused:
            await local.open_agent(on)
        assert refused.value.kind == "schedule_tools"
