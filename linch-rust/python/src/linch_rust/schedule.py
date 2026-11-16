"""Schedules and background runs: the typed results of the `schedule.*` and
`job.*` methods, and the calls `Agent` makes for them.

A schedule or a queued run is stored in the agent's Postgres database and
run there by a worker (a `linch-server` with a `worker` section), never by
this client's connection; a stdio server runs none.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Literal, NotRequired, TypedDict, cast

from ._rpc import Connection

CHECK_IN = "Check in: review what changed since your last run and act on it."
"""The default prompt of `Agent.heartbeat`."""

JobState = Literal["queued", "running", "retrying", "failed", "done", "superseded"]
"""Where a job is: waiting for a worker, held by one, waiting to be tried
again (`error` says why), failed for good, ended (its run ended or
suspended), or dropped because another owner claimed its run."""

ENDED: frozenset[str] = frozenset({"failed", "done", "superseded"})
"""The states a job never leaves."""


class Schedule(TypedDict):
    """A stored schedule. Times are RFC 3339 strings in UTC.

    Attributes:
        id: Its id, unique in the agent's namespace.
        namespace: The agent's namespace.
        queue: The worker queue its fires go to.
        prompt: The prompt of every fire's run.
        session_id: The session every fire continues (a heartbeat); absent
            when each fire starts its own session.
        cron: Its cron pattern, for a cron schedule.
        timezone: The IANA zone `cron` is read in.
        interval_ms: Its interval, for an interval schedule.
        paused: Whether it is paused.
        next_fire: Its next fire (while paused, the one it had).
        last_fire: Its latest fire, made or skipped.
        fires: Fires that queued a run.
        skips: Fires skipped because the session was busy.
        created_at: When it was created.
    """

    id: str
    namespace: str
    queue: str
    prompt: str
    session_id: NotRequired[str]
    cron: NotRequired[str]
    timezone: NotRequired[str]
    interval_ms: NotRequired[int]
    paused: bool
    next_fire: str
    last_fire: NotRequired[str]
    fires: int
    skips: int
    created_at: str


class Job(TypedDict):
    """A queued run and what became of it. Times are RFC 3339 in UTC.

    Attributes:
        job_id: Its id.
        queue: Its worker queue.
        state: Where it is (see `JobState`).
        session_id: The session of its run.
        run_id: Its run, once a worker started it; read its events with
            `Agent.events`.
        schedule_id: The schedule that made it, if one did.
        fire_at: The fire time it was made for.
        attempts: How many times a worker took it.
        failures: How many of those attempts failed.
        error: The last attempt's error, while retrying or failed.
        created_at: When it was queued.
        finished_at: When it ended.
    """

    job_id: int
    queue: str
    state: JobState
    session_id: NotRequired[str]
    run_id: NotRequired[str]
    schedule_id: NotRequired[str]
    fire_at: NotRequired[str]
    attempts: int
    failures: int
    error: NotRequired[str]
    created_at: str
    finished_at: NotRequired[str]


def _params(handle: str, **fields: Any) -> dict[str, Any]:
    params: dict[str, Any] = {"agent": handle}
    params.update({name: value for name, value in fields.items() if value is not None})
    return params


async def create(
    conn: Connection,
    handle: str,
    prompt: str,
    *,
    cron: str | None,
    timezone: str | None,
    interval_ms: int | None,
    session_id: str | None,
    id: str | None,
    queue: str | None,
) -> Schedule:
    params = _params(
        handle,
        prompt=prompt,
        cron=cron,
        timezone=timezone,
        interval_ms=interval_ms,
        session_id=session_id,
        id=id,
        queue=queue,
    )
    return cast(Schedule, await conn.request("schedule.create", params))


async def listed(conn: Connection, handle: str) -> list[Schedule]:
    answer = await conn.request("schedule.list", _params(handle))
    return list(answer["schedules"])


async def by_id(conn: Connection, method: str, handle: str, id: str) -> Any:
    return await conn.request(method, _params(handle, id=id))


async def enqueue(
    conn: Connection, handle: str, session_id: str, prompt: str, queue: str | None
) -> Job:
    params = _params(handle, session_id=session_id, prompt=prompt, queue=queue)
    return cast(Job, await conn.request("job.enqueue", params))


async def job(conn: Connection, handle: str, job_id: int) -> Job:
    return cast(Job, await conn.request("job.get", _params(handle, job_id=job_id)))


async def jobs(
    conn: Connection, handle: str, schedule_id: str | None, limit: int | None
) -> list[Job]:
    params = _params(handle, schedule_id=schedule_id, limit=limit)
    answer = await conn.request("job.list", params)
    return list(answer["jobs"])


async def wait(
    conn: Connection, handle: str, job_id: int, poll: float, timeout: float | None
) -> Job:
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        current = await job(conn, handle, job_id)
        if current["state"] in ENDED:
            return current
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError(f"job {job_id} is still {current['state']}")
        await asyncio.sleep(poll)
