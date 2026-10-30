"""Host-mounted, session-scoped schedule tools."""

from __future__ import annotations

from typing import Any, cast

from ..errors import ConfigError
from ..kernel import Disposable
from ..tools import Tool, ToolRegistry, ToolResult, tool
from .schedules import cancel_schedule, create_schedule, list_schedules


def register_schedule_tools(registry: ToolRegistry, pool: Any, *, session_id: str) -> Disposable:
    """Mount create/list/cancel tools, returning an identity-safe idempotent undo.

    The host binds the authorized session target; model input cannot select a
    different session. No Agent constructor flag or automatic registration.
    """

    async def create(
        payload: str,
        cron: str | None = None,
        interval_s: float | None = None,
        queue: str | None = None,
    ) -> ToolResult:
        """Schedule recurring work using a UTC five-field cron or interval in seconds."""
        try:
            s = await create_schedule(
                pool, session_id, payload, cron=cron, interval_s=interval_s, queue=queue
            )
        except ValueError as exc:
            return ToolResult(content=str(exc), is_error=True)
        return ToolResult(
            content=f"Scheduled {s.id} at {s.next_fire.isoformat()}",
            metadata={"id": s.id, "next_fire": s.next_fire.isoformat()},
        )

    async def listing() -> ToolResult:
        """List this session's schedule definitions."""
        schedules = await list_schedules(pool, session_id=session_id)
        return ToolResult(
            content="\n".join(
                f"{s.id}: {s.payload} enabled={s.enabled} next_fire={s.next_fire.isoformat()}"
                for s in schedules
            )
            or "No schedules registered."
        )

    async def cancel(id: str) -> ToolResult:
        """Cancel a schedule belonging to this session."""
        removed = await cancel_schedule(pool, id, session_id=session_id)
        return ToolResult(
            content=f"Cancelled {id}" if removed else f"No enabled schedule {id}",
            is_error=not removed,
        )

    tools: list[Any] = [
        tool(create, name="CreateSchedule", scope="write"),
        tool(listing, name="ListSchedules", scope="read"),
        tool(cancel, name="CancelSchedule", scope="write"),
    ]
    for t in tools:
        if registry.get(t.name) is not None:
            raise ConfigError(f"tool {t.name!r} already registered")
    disposers = [registry.register(cast(Tool, t)) for t in tools]

    async def undo() -> None:
        for d in reversed(disposers):
            await d()

    return Disposable(undo)
