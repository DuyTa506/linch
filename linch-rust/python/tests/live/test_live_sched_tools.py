"""Live: a real model schedules its own follow-ups with `CreateSchedule`,
`ListSchedules` and `CancelSchedule`; the fires run as the same agent and
principal; `max_schedules` holds. Skips without the live environment.

A model turn is retried at most once (`ask`), since a real model may skip a
tool call; every assertion is on tool events and database rows.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from live_sched_support import (
    TOKEN,
    kind,
    live_config,
    pg_dsn,
    query,
    serving,
    tenant_allow,
    until,
)

from linch_rust import Agent, Envelope, Principal, ResultEvent, ToolCallEndEvent

__all__ = ["kind", "pg_dsn"]

pytestmark = pytest.mark.live

PONG = "Reply with the single word PONG and nothing else."
ADA: Principal = {"tenant": "acme", "user": "ada", "roles": ["ops"]}


def tool_results(events: list[Envelope], name: str) -> list[ToolCallEndEvent]:
    return [
        e.event
        for e in events
        if isinstance(e.event, ToolCallEndEvent) and e.event.tool_name == name
    ]


def text_of(end: ToolCallEndEvent) -> str:
    return " ".join(block.get("text", "") for block in end.result.get("content", []))


async def ask(agent: Agent, session: str, prompt: str, name: str) -> list[Envelope]:
    """Run `prompt` and return its events; once more when the model made no
    `name` call (a flaky model turn is retried a single time)."""
    for attempt in range(2):
        run = await agent.run(session, prompt, principal=ADA)
        result = await run.result()
        assert isinstance(result, ResultEvent) and result.subtype == "success", result
        events = await agent.events(run.id)
        if tool_results(events, name) or attempt == 1:
            return events
    raise AssertionError("unreachable")


def config(kind: str, dsn: str, root: Path) -> Any:
    return live_config(
        kind,
        "acme",
        dsn,
        schedule_tools=True,
        permissions={"mode": "skip_dangerous", "project_root": str(root)},
    )


def allow(dsn: str, root: Path, **more: Any) -> dict[str, Any]:
    return {
        "acme": {
            "tokens": [{"value": TOKEN}],
            "allow": tenant_allow(dsn, workspace_roots=[str(root)], **more),
        }
    }


async def test_the_model_creates_lists_and_cancels_its_own_schedule(
    kind: str, pg_dsn: str, tmp_path: Path
) -> None:
    root = tmp_path.resolve()
    async with serving(pg_dsn, tenants=allow(pg_dsn, root)) as live, live.connect() as server:
        agent = await server.open_agent(config(kind, pg_dsn, root))

        events = await ask(
            agent,
            "tools-1",
            f"Call the tool CreateSchedule with payload {PONG!r} and interval_s 4. "
            "Then answer: scheduled.",
            "CreateSchedule",
        )
        [created] = tool_results(events, "CreateSchedule")
        assert not created.is_error, text_of(created)
        found = re.match(r"Scheduled (\S+) at ", text_of(created))
        assert found, text_of(created)
        schedule_id = found.group(1)

        [stored] = await agent.schedules()
        assert stored["id"] == schedule_id
        assert stored.get("session_id") == "tools-1", "the calling run's session"
        assert stored.get("interval_ms") == 4000

        # It fires, as the same agent and the same principal.
        async def fired() -> list[Any]:
            return [j for j in await agent.jobs(schedule_id=schedule_id) if j["state"] == "done"]

        [first, *_] = await until(fired, timeout=60, every=1, what="a fire of the model's schedule")
        assert first.get("session_id") == "tools-1"
        fire_events = await agent.events(first["run_id"])
        last = fire_events[-1].event
        assert isinstance(last, ResultEvent) and last.subtype == "success"
        [(principal, digest)] = query(
            pg_dsn,
            "SELECT principal, agent FROM linch_worker_schedules WHERE id = %s",
            schedule_id,
        )
        assert "ada" in str(principal) and "ops" in str(principal)
        assert digest, "the schedule names the stored agent config"
        [(job_digest,)] = query(
            pg_dsn,
            "SELECT agent FROM linch_worker_history WHERE run_id = %s",
            first["run_id"],
        )
        assert job_digest == digest, "the fire runs the config the schedule was made with"

        # Stop the heartbeat and let its last fire end, so no fire is in the
        # session while the next asks run in it (a busy session is refused).
        await agent.pause_schedule(schedule_id)

        async def idle() -> bool:
            return all(j["state"] in ("done", "failed") for j in await agent.jobs())

        await until(idle, timeout=60, every=1, what="the heartbeat's jobs to end")

        listed = await ask(
            agent, "tools-1", "Call the tool ListSchedules, then answer: listed.", "ListSchedules"
        )
        [lines] = tool_results(listed, "ListSchedules")
        assert schedule_id in text_of(lines)

        cancelled = await ask(
            agent,
            "tools-1",
            f"Call the tool CancelSchedule with id {schedule_id!r}, then answer: cancelled.",
            "CancelSchedule",
        )
        [gone] = tool_results(cancelled, "CancelSchedule")
        assert not gone.is_error, text_of(gone)
        assert await agent.schedules() == []


async def test_max_schedules_holds_against_the_model(
    kind: str, pg_dsn: str, tmp_path: Path
) -> None:
    root = tmp_path.resolve()
    tenants = allow(pg_dsn, root, max_schedules=1)
    async with serving(pg_dsn, tenants=tenants) as live, live.connect() as server:
        agent = await server.open_agent(config(kind, pg_dsn, root))
        events = await ask(
            agent,
            "cap-1",
            "Call the tool CreateSchedule with payload 'first' and interval_s 600. "
            "Then call CreateSchedule again with payload 'second' and interval_s 700. "
            "Then answer: done.",
            "CreateSchedule",
        )
        results = tool_results(events, "CreateSchedule")
        assert len(results) == 2, [text_of(r) for r in results]
        assert [r.is_error for r in results] == [False, True]
        assert results[1].error_code == "limit_reached", results[1]
        assert len(await agent.schedules()) == 1, "the cap holds"
