"""Register / list / cancel schedule tools, bound to a :class:`ScheduleStore`.

Auto-registered by ``Agent(schedule_store=...)``. Creating a schedule validates
the cron expression at register time, so an invalid expression is rejected
before it can ever fire.
"""

from __future__ import annotations

import time
from typing import Any

from ...tools import ToolResult, tool
from .schedule import Schedule
from .store import ScheduleStore


def schedule_tools(store: ScheduleStore, *, clock: Any = time.time) -> list[Any]:
    """Build the [create, list, cancel] schedule tools over *store*."""

    async def create_schedule(
        payload: str,
        cron: str | None = None,
        interval_s: float | None = None,
    ) -> ToolResult:
        """Register a recurring trigger. Provide exactly one of cron or interval_s.

        cron is a 5-field expression (minute hour day-of-month month day-of-week),
        evaluated in UTC. payload is delivered verbatim when the schedule fires.
        """
        try:
            schedule = Schedule(payload=payload, cron=cron, interval_s=interval_s)
        except ValueError as exc:
            return ToolResult(content=f"Invalid schedule: {exc}", is_error=True)
        now = clock()
        schedule.created_at = now
        schedule.next_run = schedule.compute_next_run(now)
        await store.add(schedule)
        return ToolResult(
            content=f"Scheduled {schedule.id} (next run at epoch {schedule.next_run:.0f}).",
            summary="schedule created",
            metadata={"id": schedule.id, "next_run": schedule.next_run},
        )

    async def list_schedules() -> ToolResult:
        """List all registered schedules."""
        schedules = await store.list()
        if not schedules:
            return ToolResult(content="No schedules registered.", summary="0 schedules")
        lines = [
            f"[{s.id}] {'cron=' + s.cron if s.cron else 'every ' + str(s.interval_s) + 's'}"
            f" enabled={s.enabled} next_run={s.next_run}"
            for s in schedules
        ]
        return ToolResult(
            content="\n".join(lines),
            summary=f"{len(schedules)} schedule(s)",
            metadata={"ids": [s.id for s in schedules]},
        )

    async def cancel_schedule(id: str) -> ToolResult:
        """Cancel and remove a schedule by id."""
        removed = await store.remove(id)
        if not removed:
            return ToolResult(content=f"No schedule with id {id}.", is_error=True)
        return ToolResult(content=f"Cancelled schedule {id}.", summary="schedule cancelled")

    return [
        tool(create_schedule, name="CreateSchedule", scope="write"),
        tool(list_schedules, name="ListSchedules", scope="read"),
        tool(cancel_schedule, name="CancelSchedule", scope="write"),
    ]
