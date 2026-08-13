"""Durable :class:`ScheduleStore` backed by SQLite.

Mirrors :class:`~linch.memory.sqlite.SqliteMemoryStore`: all DB I/O runs on a
single dedicated worker thread so the event loop never blocks. Durable schedules
survive a process restart / store reload. Due schedules can be claimed
transactionally so multiple scheduler workers sharing the same database do not
fire the same tick twice.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

from ...storage._executor import SqliteExecutor
from .schedule import Schedule, ScheduleOccurrence, occurrence_id
from .store import _validate_occurrence_claim_args


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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schedule_occurrences (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            schedule_id TEXT NOT NULL,
            payload TEXT NOT NULL,
            scheduled_for REAL NOT NULL,
            metadata TEXT NOT NULL,
            materialized_at REAL NOT NULL,
            lease_token TEXT UNIQUE,
            lease_owner TEXT,
            lease_expires_at REAL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_schedule_occurrences_delivery
        ON schedule_occurrences(scheduled_for, seq)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_schedule_occurrences_lease_expiry
        ON schedule_occurrences(lease_expires_at)
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

    async def claim_due(self, now: float) -> list[Schedule]:
        return await self._exec.run(lambda conn: _claim_due(conn, now))

    async def claim_due_occurrences(
        self,
        now: float,
        *,
        owner: str,
        lease_s: float,
        limit: int | None = None,
    ) -> list[ScheduleOccurrence]:
        _validate_occurrence_claim_args(owner=owner, lease_s=lease_s, now=now, limit=limit)
        return await self._exec.run(
            lambda conn: _claim_due_occurrences(
                conn,
                now,
                owner=owner,
                lease_s=lease_s,
                limit=limit,
            )
        )

    async def ack_occurrences(self, occurrences: Sequence[ScheduleOccurrence]) -> None:
        if occurrences:
            await self._exec.run(lambda conn: _ack_occurrences(conn, occurrences))

    async def release_occurrences(self, occurrences: Sequence[ScheduleOccurrence]) -> None:
        if occurrences:
            await self._exec.run(lambda conn: _release_occurrences(conn, occurrences))

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


def _claim_due(conn: sqlite3.Connection, now: float) -> list[Schedule]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = _fetch_due(conn, now)
        schedules = [_row_to_schedule(row) for row in rows]
        for schedule in schedules:
            schedule.next_run = schedule.compute_next_run(now)
            conn.execute(
                "UPDATE schedules SET next_run = ? WHERE id = ?",
                (schedule.next_run, schedule.id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return schedules


def _fetch_due(conn: sqlite3.Connection, now: float) -> list[dict[str, Any]]:
    cursor = conn.execute(
        """
        SELECT * FROM schedules
        WHERE enabled = 1
          AND next_run IS NOT NULL
          AND next_run <= ?
        ORDER BY next_run ASC, id ASC
        """,
        (now,),
    )
    return [dict(row) for row in cursor.fetchall()]


def _claim_due_occurrences(
    conn: sqlite3.Connection,
    now: float,
    *,
    owner: str,
    lease_s: float,
    limit: int | None,
) -> list[ScheduleOccurrence]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        # First commit-worthy action is the occurrence outbox write.  It shares
        # this transaction with advancing next_run, eliminating the historical
        # crash window between those two operations.
        for row in _fetch_due(conn, now):
            schedule = _row_to_schedule(row)
            assert schedule.next_run is not None
            scheduled_for = schedule.next_run
            stable_id = occurrence_id(schedule.id, scheduled_for)
            conn.execute(
                """
                INSERT OR IGNORE INTO schedule_occurrences(
                    id, schedule_id, payload, scheduled_for, metadata, materialized_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    stable_id,
                    schedule.id,
                    schedule.payload,
                    scheduled_for,
                    json.dumps(schedule.metadata, sort_keys=True),
                    now,
                ),
            )
            conn.execute(
                "UPDATE schedules SET next_run = ? WHERE id = ?",
                (schedule.compute_next_run(now), schedule.id),
            )

        rows = _fetch_available_occurrences(conn, now, limit=limit)
        claimed: list[ScheduleOccurrence] = []
        for row in rows:
            token = uuid4().hex
            expires_at = now + lease_s
            conn.execute(
                """
                UPDATE schedule_occurrences
                SET lease_token = ?, lease_owner = ?, lease_expires_at = ?
                WHERE id = ?
                """,
                (token, owner, expires_at, str(row["id"])),
            )
            claimed.append(
                _row_to_occurrence(
                    row,
                    token=token,
                    owner=owner,
                    expires_at=expires_at,
                )
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return claimed


def _fetch_available_occurrences(
    conn: sqlite3.Connection, now: float, *, limit: int | None
) -> list[dict[str, Any]]:
    sql = """
        SELECT * FROM schedule_occurrences
        WHERE lease_token IS NULL
           OR lease_expires_at IS NULL
           OR lease_expires_at <= ?
        ORDER BY scheduled_for ASC, seq ASC
    """
    params: list[Any] = [now]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    cursor = conn.execute(sql, params)
    return [dict(row) for row in cursor.fetchall()]


def _row_to_occurrence(
    row: dict[str, Any], *, token: str, owner: str, expires_at: float
) -> ScheduleOccurrence:
    return ScheduleOccurrence(
        id=str(row["id"]),
        schedule_id=str(row["schedule_id"]),
        payload=str(row["payload"]),
        scheduled_for=float(row["scheduled_for"]),
        metadata=dict(json.loads(row["metadata"] or "{}")),
        materialized_at=float(row["materialized_at"]),
        token=token,
        owner=owner,
        expires_at=expires_at,
    )


def _ack_occurrences(conn: sqlite3.Connection, occurrences: Sequence[ScheduleOccurrence]) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.executemany(
            "DELETE FROM schedule_occurrences WHERE id = ? AND lease_token = ?",
            [(occurrence.id, occurrence.token) for occurrence in occurrences],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _release_occurrences(
    conn: sqlite3.Connection, occurrences: Sequence[ScheduleOccurrence]
) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.executemany(
            """
            UPDATE schedule_occurrences
            SET lease_token = NULL, lease_owner = NULL, lease_expires_at = NULL
            WHERE id = ? AND lease_token = ?
            """,
            [(occurrence.id, occurrence.token) for occurrence in occurrences],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
