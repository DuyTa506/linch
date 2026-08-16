"""``ScheduleStore`` protocol + an in-memory implementation.

Durable adapters (e.g. :class:`~linch.scheduling.sqlite.SqliteScheduleStore`)
implement the same protocol so schedules survive a process restart.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from .schedule import Schedule, ScheduleOccurrence, occurrence_id


@runtime_checkable
class ScheduleStore(Protocol):
    async def add(self, schedule: Schedule) -> None: ...
    async def update(self, schedule: Schedule) -> None: ...
    async def remove(self, schedule_id: str) -> bool: ...
    async def get(self, schedule_id: str) -> Schedule | None: ...
    async def list(self) -> list[Schedule]: ...


@runtime_checkable
class ClaimingScheduleStore(ScheduleStore, Protocol):
    """Optional extension for stores that can atomically claim due schedules."""

    async def claim_due(self, now: float) -> list[Schedule]: ...


@runtime_checkable
class LeasedScheduleStore(ScheduleStore, Protocol):
    """Optional durable occurrence-outbox extension for schedule delivery."""

    async def claim_due_occurrences(
        self,
        now: float,
        *,
        owner: str,
        lease_s: float,
        limit: int | None = None,
    ) -> list[ScheduleOccurrence]: ...

    async def ack_occurrences(self, occurrences: Sequence[ScheduleOccurrence]) -> None: ...

    async def release_occurrences(self, occurrences: Sequence[ScheduleOccurrence]) -> None: ...


@dataclass(slots=True)
class _OccurrenceEntry:
    id: str
    schedule_id: str
    payload: str
    scheduled_for: float
    metadata: dict[str, Any]
    materialized_at: float
    token: str | None = None
    owner: str | None = None
    expires_at: float | None = None


class InMemoryScheduleStore:
    """Process-local schedule store guarded by an ``asyncio.Lock``."""

    def __init__(self) -> None:
        self._items: dict[str, Schedule] = {}
        self._occurrences: dict[str, _OccurrenceEntry] = {}
        self._lock = asyncio.Lock()

    async def add(self, schedule: Schedule) -> None:
        async with self._lock:
            self._items[schedule.id] = schedule

    async def update(self, schedule: Schedule) -> None:
        async with self._lock:
            self._items[schedule.id] = schedule

    async def remove(self, schedule_id: str) -> bool:
        async with self._lock:
            return self._items.pop(schedule_id, None) is not None

    async def get(self, schedule_id: str) -> Schedule | None:
        async with self._lock:
            return self._items.get(schedule_id)

    async def list(self) -> list[Schedule]:
        async with self._lock:
            return list(self._items.values())

    async def claim_due(self, now: float) -> list[Schedule]:
        async with self._lock:
            claimed: list[Schedule] = []
            for schedule in self._items.values():
                if not schedule.enabled or schedule.next_run is None:
                    continue
                if schedule.next_run > now:
                    continue
                schedule.next_run = schedule.compute_next_run(now)
                claimed.append(schedule)
            return claimed

    async def claim_due_occurrences(
        self,
        now: float,
        *,
        owner: str,
        lease_s: float,
        limit: int | None = None,
    ) -> list[ScheduleOccurrence]:
        _validate_occurrence_claim_args(owner=owner, lease_s=lease_s, now=now, limit=limit)
        async with self._lock:
            # Materialization and schedule advancement share this critical
            # section. Cancellation can happen only before or after both.
            due = sorted(
                (
                    schedule
                    for schedule in self._items.values()
                    if schedule.enabled
                    and schedule.next_run is not None
                    and schedule.next_run <= now
                ),
                key=lambda schedule: (schedule.next_run or 0.0, schedule.id),
            )
            for schedule in due:
                assert schedule.next_run is not None
                scheduled_for = schedule.next_run
                stable_id = occurrence_id(schedule.id, scheduled_for)
                self._occurrences.setdefault(
                    stable_id,
                    _OccurrenceEntry(
                        id=stable_id,
                        schedule_id=schedule.id,
                        payload=schedule.payload,
                        scheduled_for=scheduled_for,
                        metadata=dict(schedule.metadata),
                        materialized_at=now,
                    ),
                )
                schedule.next_run = schedule.compute_next_run(now)

            if limit == 0:
                return []
            available = sorted(
                (
                    occurrence
                    for occurrence in self._occurrences.values()
                    if occurrence.token is None
                    or occurrence.expires_at is None
                    or occurrence.expires_at <= now
                ),
                key=lambda occurrence: (occurrence.scheduled_for, occurrence.id),
            )
            claimed: list[ScheduleOccurrence] = []
            for entry in available:
                token = uuid4().hex
                expires_at = now + lease_s
                entry.token = token
                entry.owner = owner
                entry.expires_at = expires_at
                claimed.append(_to_occurrence(entry))
                if limit is not None and len(claimed) >= limit:
                    break
            return claimed

    async def ack_occurrences(self, occurrences: Sequence[ScheduleOccurrence]) -> None:
        if not occurrences:
            return
        capabilities = {(occurrence.id, occurrence.token) for occurrence in occurrences}
        async with self._lock:
            for occurrence_id_, entry in list(self._occurrences.items()):
                if (occurrence_id_, entry.token or "") in capabilities:
                    del self._occurrences[occurrence_id_]

    async def release_occurrences(self, occurrences: Sequence[ScheduleOccurrence]) -> None:
        if not occurrences:
            return
        capabilities = {(occurrence.id, occurrence.token) for occurrence in occurrences}
        async with self._lock:
            for entry in self._occurrences.values():
                if (entry.id, entry.token or "") in capabilities:
                    entry.token = None
                    entry.owner = None
                    entry.expires_at = None

    def dump(self) -> list[dict[str, Any]]:
        """Serialize all schedules (handy for tests / lightweight persistence)."""
        return [s.to_dict() for s in self._items.values()]

    @classmethod
    def load(cls, rows: list[dict[str, Any]]) -> InMemoryScheduleStore:
        store = cls()
        for row in rows:
            schedule = Schedule.from_dict(row)
            store._items[schedule.id] = schedule
        return store


def _to_occurrence(entry: _OccurrenceEntry) -> ScheduleOccurrence:
    assert entry.token is not None
    assert entry.owner is not None
    assert entry.expires_at is not None
    return ScheduleOccurrence(
        id=entry.id,
        schedule_id=entry.schedule_id,
        payload=entry.payload,
        scheduled_for=entry.scheduled_for,
        metadata=dict(entry.metadata),
        materialized_at=entry.materialized_at,
        token=entry.token,
        owner=entry.owner,
        expires_at=entry.expires_at,
    )


def _validate_occurrence_claim_args(
    *, owner: str, lease_s: float, now: float, limit: int | None
) -> None:
    if not isinstance(owner, str) or not owner:
        raise ValueError("owner must be non-empty")
    if isinstance(lease_s, bool) or not isinstance(lease_s, (int, float)):
        raise ValueError("lease_s must be a positive finite number")
    if not math.isfinite(lease_s) or lease_s <= 0:
        raise ValueError("lease_s must be a positive finite number")
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
        raise ValueError("now must be finite")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 0):
        raise ValueError("limit must be a non-negative integer or None")
