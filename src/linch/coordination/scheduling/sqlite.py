"""Durable :class:`ScheduleStore` backed by SQLite.

Mirrors :class:`~linch.memory.sqlite.SqliteMemoryStore`: all DB I/O runs on a
single dedicated worker thread so the event loop never blocks. Durable schedules
survive a process restart / store reload. A multi-process leader-election lock
(so only one of N processes fires a given schedule) is intentionally out of
scope here and left to the embedder.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from ...storage._executor import SqliteExecutor
from .schedule import Schedule


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schedules (
            id TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            cron TEXT,
            interval_s REAL,
            next_run REAL,
            enabled INTEGER NOT NULL,
            created_at REAL,
            metadata TEXT NOT NULL
        )
        """
    )


def _row_to_schedule(row: dict[str, Any]) -> Schedule:
    return Schedule.from_dict(
        {
            "id": row["id"],
            "payload": row["payload"],
            "cron": row["cron"],
            "interval_s": row["interval_s"],
            "next_run": row["next_run"],
            "enabled": bool(row["enabled"]),
            "created_at": row["created_at"],
            "metadata": json.loads(row["metadata"] or "{}"),
        }
    )


def _schedule_to_row(schedule: Schedule) -> tuple[Any, ...]:
    return (
        schedule.id,
        schedule.payload,
        schedule.cron,
        schedule.interval_s,
        schedule.next_run,
        1 if schedule.enabled else 0,
        schedule.created_at,
        json.dumps(schedule.metadata, sort_keys=True),
    )


class SqliteScheduleStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._exec = SqliteExecutor(self.path, init=_init_schema, wal=True)

    async def add(self, schedule: Schedule) -> None:
        await self.update(schedule)

    async def update(self, schedule: Schedule) -> None:
        await self._exec.run(lambda conn: _upsert(conn, _schedule_to_row(schedule)))

    async def remove(self, schedule_id: str) -> bool:
        return await self._exec.run(lambda conn: _delete(conn, schedule_id))

    async def get(self, schedule_id: str) -> Schedule | None:
        row = await self._exec.run(lambda conn: _fetch_one(conn, schedule_id))
        return _row_to_schedule(row) if row else None

    async def list(self) -> list[Schedule]:
        rows = await self._exec.run(_fetch_all)
        return [_row_to_schedule(row) for row in rows]

    async def aclose(self) -> None:
        await self._exec.close()

    def close(self) -> None:
        self._exec.close_sync()

    def __enter__(self) -> SqliteScheduleStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    async def __aenter__(self) -> SqliteScheduleStore:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()


def _upsert(conn: sqlite3.Connection, row: tuple[Any, ...]) -> None:
    conn.execute(
        """
        INSERT INTO schedules(
            id, payload, cron, interval_s, next_run, enabled, created_at, metadata
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            payload=excluded.payload,
            cron=excluded.cron,
            interval_s=excluded.interval_s,
            next_run=excluded.next_run,
            enabled=excluded.enabled,
            created_at=excluded.created_at,
            metadata=excluded.metadata
        """,
        row,
    )
    conn.commit()


def _delete(conn: sqlite3.Connection, schedule_id: str) -> bool:
    cursor = conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
    conn.commit()
    return cursor.rowcount > 0


def _fetch_one(conn: sqlite3.Connection, schedule_id: str) -> dict[str, Any] | None:
    cursor = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,))
    row = cursor.fetchone()
    return dict(row) if row else None


def _fetch_all(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    cursor = conn.execute("SELECT * FROM schedules")
    return [dict(row) for row in cursor.fetchall()]
