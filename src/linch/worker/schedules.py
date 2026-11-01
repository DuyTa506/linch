"""Durable UTC schedule definitions and one deferred PgQueuer job per fire."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from croniter import croniter
from pgqueuer import Queries

from .dispatch import RunRequest, _enqueue, _entrypoint, _traceparent


@dataclass(frozen=True, slots=True)
class ScheduleDefinition:
    """Describe a durable UTC schedule and its next deferred fire.

    Args:
        id: Stable schedule identifier.
        session_id: Session receiving each materialized run.
        payload: Prompt or payload submitted for each fire.
        next_fire: UTC timestamp of the next scheduled occurrence.
        cron: Optional five-field cron expression.
        interval_s: Optional positive interval in seconds.
        enabled: Whether future occurrences remain enabled.
        queue: Optional worker queue for materialized runs.

    Attributes:
        id: Stable schedule identifier.
        session_id: Target session identifier.
        payload: Repeated run payload.
        next_fire: Next occurrence timestamp.
        cron: Cron expression, when configured.
        interval_s: Interval in seconds, when configured.
        enabled: Future-fire enablement flag.
        queue: Optional worker queue.
    """

    id: str
    session_id: str
    payload: str
    next_fire: datetime
    cron: str | None = None
    interval_s: float | None = None
    enabled: bool = True
    queue: str | None = None


def _next(cron: str | None, interval_s: float | None, after: datetime) -> datetime:
    if (cron is None) == (interval_s is None):
        raise ValueError("provide exactly one of cron or interval_s")
    if after.tzinfo is None:
        raise ValueError("after must be timezone-aware")
    after = after.astimezone(UTC)
    if cron is not None:
        if len(cron.split()) != 5 or not croniter.is_valid(cron):
            raise ValueError("cron must be a valid five-field expression")
        return croniter(cron, after).get_next(datetime)
    if (
        isinstance(interval_s, bool)
        or interval_s is None
        or not math.isfinite(interval_s)
        or interval_s <= 0
    ):
        raise ValueError("interval_s must be positive and finite")
    result = after + timedelta(seconds=interval_s)
    if result <= after:
        raise ValueError("interval_s is below datetime precision")
    return result


def _definition(row: Any) -> ScheduleDefinition:
    return ScheduleDefinition(
        **{name: row[name] for name in ScheduleDefinition.__dataclass_fields__}
    )


async def _defer(conn: Any, id: str, fire: datetime) -> None:
    # execute_after is a relative duration in PgQueuer 1.6, evaluated by SQL
    # against the transaction timestamp. Use the database clock for the offset.
    now = await conn.fetchval("SELECT transaction_timestamp()")
    await Queries.from_asyncpg_connection(conn).enqueue(
        "linch.schedule",
        json.dumps({"schedule_id": id, "fire": fire.isoformat()}).encode(),
        execute_after=fire - now,
        dedupe_key=f"schedule:{id}:{fire.isoformat()}",
        on_conflict="skip",
    )


async def create_schedule(
    pool: Any,
    session_id: str,
    payload: str,
    *,
    cron: str | None = None,
    interval_s: float | None = None,
    queue: str | None = None,
    id: str | None = None,
    after: datetime | None = None,
) -> ScheduleDefinition:
    """Persist a definition and its first deferred fire atomically. Cron is UTC.

    Args:
        pool: Host-owned asyncpg pool used for the schedule transaction.
        session_id: Session receiving each materialized run.
        payload: Prompt or payload submitted for each fire.
        cron: Optional valid five-field UTC cron expression.
        interval_s: Optional positive interval in seconds.
        queue: Optional worker queue for materialized runs.
        id: Optional caller-supplied schedule identifier.
        after: Optional timezone-aware timestamp from which to calculate the first fire.

    Returns:
        The persisted schedule definition with its first fire timestamp.

    Raises:
        ValueError: If cron/interval settings are invalid or both are supplied.
    """
    _entrypoint(queue)
    fire = _next(cron, interval_s, after or datetime.now(UTC))
    sid = id or str(uuid4())
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO linch_worker_schedules
                    (id,session_id,payload,cron,interval_s,queue,next_fire,traceparent)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING *
            """,
                sid,
                session_id,
                payload,
                cron,
                interval_s,
                queue,
                fire,
                _traceparent(),
            )
            await _defer(conn, sid, fire)
    return _definition(row)


async def list_schedules(pool: Any, *, session_id: str) -> list[ScheduleDefinition]:
    """List a session's enabled and cancelled definitions.

    Args:
        pool: Host-owned asyncpg pool used to query schedules.
        session_id: Session whose schedule definitions should be listed.

    Returns:
        Schedule definitions ordered by stable identifier.
    """
    rows = await pool.fetch(
        "SELECT * FROM linch_worker_schedules WHERE session_id=$1 ORDER BY id", session_id
    )
    return [_definition(row) for row in rows]


async def cancel_schedule(pool: Any, id: str, *, session_id: str) -> bool:
    """Disable future fires while leaving already materialized runs queued.

    Args:
        pool: Host-owned asyncpg pool used to update the schedule.
        id: Schedule identifier to disable.
        session_id: Session that owns the schedule.

    Returns:
        ``True`` when an enabled schedule was disabled; otherwise ``False``.
    """
    return bool(
        await pool.fetchval(
            """
        UPDATE linch_worker_schedules SET enabled=FALSE
        WHERE id=$1 AND session_id=$2 AND enabled RETURNING TRUE
    """,
            id,
            session_id,
        )
    )


async def _fire(pool: Any, payload: dict[str, Any]) -> None:
    fire = datetime.fromisoformat(payload["fire"])
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM linch_worker_schedules WHERE id=$1 FOR UPDATE",
                payload["schedule_id"],
            )
            if row is None or not row["enabled"] or row["next_fire"] != fire:
                return  # Ack cancelled definitions and replayed occurrences.
            rid = f"schedule:{row['id']}:{fire.isoformat()}"
            await _enqueue(
                conn,
                [RunRequest(row["session_id"], row["payload"], rid, row["queue"])],
                row["traceparent"],
            )
            next_fire = _next(row["cron"], row["interval_s"], fire)
            await conn.execute(
                "UPDATE linch_worker_schedules SET next_fire=$2 WHERE id=$1", row["id"], next_fire
            )
            await _defer(conn, row["id"], next_fire)
