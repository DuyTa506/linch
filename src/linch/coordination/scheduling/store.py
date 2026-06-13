"""``ScheduleStore`` protocol + an in-memory implementation.

Durable adapters (e.g. :class:`~linch.scheduling.sqlite.SqliteScheduleStore`)
implement the same protocol so schedules survive a process restart.
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol, runtime_checkable

from .schedule import Schedule


@runtime_checkable
class ScheduleStore(Protocol):
    async def add(self, schedule: Schedule) -> None: ...
    async def update(self, schedule: Schedule) -> None: ...
    async def remove(self, schedule_id: str) -> bool: ...
    async def get(self, schedule_id: str) -> Schedule | None: ...
    async def list(self) -> list[Schedule]: ...


class InMemoryScheduleStore:
    """Process-local schedule store guarded by an ``asyncio.Lock``."""

    def __init__(self) -> None:
        self._items: dict[str, Schedule] = {}
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
