"""PostgreSQL-backed :class:`~linch.sessions.store.SessionStore`.

Durable, multi-writer, and connection-pooled via ``asyncpg``.  Multiple
worker processes can share the same Postgres database; each gets its own
pool of connections so concurrent turns proceed in parallel (not serialised
on a single connection like SQLite).

Install::

    pip install 'linch[postgres]'

Usage::

    from linch.sessions.postgres import PostgresSessionStore

    store = PostgresSessionStore("postgresql://user:pw@host/db")
    agent = Agent(model="...", session_store=store)
    ...
    await store.close()

Or pass a pre-created ``asyncpg.Pool`` if you manage pool lifecycle yourself::

    pool = await asyncpg.create_pool(dsn)
    store = PostgresSessionStore(dsn="", pool=pool)
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import uuid4

from linch.sessions.memory import now_iso
from linch.sessions.store import SessionRecord, StoredMessage
from linch.sessions.tasks import CreateTaskInput, Task, TaskPatch
from linch.types import Message, message_from_dict, message_to_dict

from ..storage._pg import _import_asyncpg

# ── DDL ─────────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    meta        TEXT NOT NULL DEFAULT '{}',
    invoked_skills TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS messages (
    session_id  TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    appended_at TEXT NOT NULL,
    message     TEXT NOT NULL,
    PRIMARY KEY (session_id, seq)
);

CREATE TABLE IF NOT EXISTS task_counters (
    session_id  TEXT PRIMARY KEY,
    next_id     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    session_id   TEXT NOT NULL,
    id           TEXT NOT NULL,
    subject      TEXT NOT NULL,
    description  TEXT NOT NULL,
    active_form  TEXT,
    status       TEXT NOT NULL,
    owner        TEXT,
    metadata_json TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (session_id, id)
);

CREATE TABLE IF NOT EXISTS task_edges (
    session_id   TEXT NOT NULL,
    from_task_id TEXT NOT NULL,
    to_task_id   TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'blocks',
    PRIMARY KEY (session_id, from_task_id, to_task_id)
);
"""


# ── Store ────────────────────────────────────────────────────────────────────


class PostgresSessionStore:
    """Session store backed by Postgres via ``asyncpg``.

    All operations use a shared connection pool.  Concurrent coroutines
    (including subagents) acquire independent connections and run in parallel —
    no single-connection serialisation bottleneck.

    The schema is created automatically on first use (``CREATE TABLE IF NOT
    EXISTS``).  No migration tool is required; simply point at an empty DB.

    :param dsn: PostgreSQL connection string
        (e.g. ``"postgresql://user:pw@host/dbname"``).
    :param pool: Pass a pre-created ``asyncpg.Pool`` to reuse an existing pool.
        When provided, *dsn* is ignored.
    :param min_size: Minimum pool connections (default 1).
    :param max_size: Maximum pool connections (default 10).
    """

    def __init__(
        self,
        dsn: str,
        *,
        pool: Any = None,
        min_size: int = 1,
        max_size: int = 10,
    ) -> None:
        _import_asyncpg()  # fail fast with install hint if asyncpg missing
        self._dsn = dsn
        self._pool: Any = pool
        self._min_size = min_size
        self._max_size = max_size
        self._initialized = False
        self._init_lock = asyncio.Lock()

    async def _ensure(self) -> Any:
        """Return the pool, creating it and the schema on first call."""
        if self._initialized:
            return self._pool
        async with self._init_lock:
            if self._initialized:
                return self._pool
            asyncpg = _import_asyncpg()
            if self._pool is None:
                self._pool = await asyncpg.create_pool(
                    self._dsn,
                    min_size=self._min_size,
                    max_size=self._max_size,
                )
            try:
                async with self._pool.acquire() as conn:
                    async with conn.transaction():
                        await conn.execute(_SCHEMA)
            except Exception:
                # Schema creation failed — close and reset so the next caller
                # retries from a clean state rather than reusing a partial pool.
                pool, self._pool = self._pool, None
                await pool.close()
                raise
            self._initialized = True
        return self._pool

    # ── SessionStore protocol ────────────────────────────────────────────────

    async def create(
        self, *, id: str | None = None, meta: dict[str, object] | None = None
    ) -> SessionRecord:
        pool = await self._ensure()
        _meta = meta or {}
        sid = id or str(uuid4())
        ts = now_iso()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, created_at, updated_at, meta, invoked_skills "
                "FROM sessions WHERE id = $1",
                sid,
            )
            if row:
                return _record(row)
            await conn.execute(
                "INSERT INTO sessions (id, created_at, updated_at, meta, invoked_skills) "
                "VALUES ($1, $2, $3, $4, $5)",
                sid,
                ts,
                ts,
                json.dumps(_meta),
                "[]",
            )
            await conn.execute(
                "INSERT INTO task_counters (session_id, next_id) VALUES ($1, 1) "
                "ON CONFLICT DO NOTHING",
                sid,
            )
        return SessionRecord(id=sid, created_at=ts, updated_at=ts, meta=_meta)

    async def load(self, id: str) -> SessionRecord | None:
        pool = await self._ensure()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, created_at, updated_at, meta, invoked_skills "
                "FROM sessions WHERE id = $1",
                id,
            )
        return _record(row) if row else None

    async def load_messages(self, id: str) -> list[StoredMessage]:
        pool = await self._ensure()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT seq, appended_at, message FROM messages WHERE session_id = $1 ORDER BY seq",
                id,
            )
        return [
            StoredMessage(
                seq=row["seq"],
                appended_at=row["appended_at"],
                message=message_from_dict(json.loads(row["message"])),
            )
            for row in rows
        ]

    async def append_messages(self, id: str, messages: list[Message]) -> list[StoredMessage]:
        pool = await self._ensure()
        ts = now_iso()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT COALESCE(MAX(seq), 0) AS cur FROM messages WHERE session_id = $1",
                    id,
                )
                cur_seq: int = row["cur"]
                stored: list[StoredMessage] = []
                for msg in messages:
                    cur_seq += 1
                    await conn.execute(
                        "INSERT INTO messages (session_id, seq, appended_at, message) "
                        "VALUES ($1, $2, $3, $4)",
                        id,
                        cur_seq,
                        ts,
                        json.dumps(message_to_dict(msg)),
                    )
                    stored.append(StoredMessage(seq=cur_seq, appended_at=ts, message=msg))
                await conn.execute("UPDATE sessions SET updated_at = $1 WHERE id = $2", ts, id)
        return stored

    async def update_meta(self, id: str, meta: dict[str, object]) -> SessionRecord:
        pool = await self._ensure()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT id, created_at, updated_at, meta, invoked_skills "
                    "FROM sessions WHERE id = $1",
                    id,
                )
                if row is None:
                    raise KeyError(f"session not found: {id}")
                existing = json.loads(row["meta"])
                existing.update(meta)
                ts = now_iso()
                await conn.execute(
                    "UPDATE sessions SET updated_at = $1, meta = $2 WHERE id = $3",
                    ts,
                    json.dumps(existing),
                    id,
                )
        return SessionRecord(
            id=row["id"],
            created_at=row["created_at"],
            updated_at=ts,
            meta=existing,
            invoked_skills=list(json.loads(row["invoked_skills"] or "[]")),
        )

    async def set_invoked_skills(self, id: str, skills: list[dict[str, object]]) -> None:
        pool = await self._ensure()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE sessions SET invoked_skills = $1, updated_at = $2 WHERE id = $3",
                json.dumps(skills),
                now_iso(),
                id,
            )

    async def list(self, *, limit: int | None = None, offset: int = 0) -> list[SessionRecord]:
        pool = await self._ensure()
        async with pool.acquire() as conn:
            if limit is not None:
                rows = await conn.fetch(
                    "SELECT id, created_at, updated_at, meta, invoked_skills "
                    "FROM sessions ORDER BY updated_at DESC LIMIT $1 OFFSET $2",
                    limit,
                    offset,
                )
            else:
                rows = await conn.fetch(
                    "SELECT id, created_at, updated_at, meta, invoked_skills "
                    "FROM sessions ORDER BY updated_at DESC"
                )
        return [_record(row) for row in rows]

    async def delete(self, id: str) -> None:
        pool = await self._ensure()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM task_edges WHERE session_id = $1", id)
                await conn.execute("DELETE FROM tasks WHERE session_id = $1", id)
                await conn.execute("DELETE FROM task_counters WHERE session_id = $1", id)
                await conn.execute("DELETE FROM messages WHERE session_id = $1", id)
                await conn.execute("DELETE FROM sessions WHERE id = $1", id)

    async def create_task(self, session_id: str, input: CreateTaskInput) -> Task:
        pool = await self._ensure()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT next_id FROM task_counters WHERE session_id = $1",
                    session_id,
                )
                next_id = int(row["next_id"]) if row else 1
                task_id = str(next_id)
                now = now_iso()
                await conn.execute(
                    """
                    INSERT INTO tasks (
                        session_id, id, subject, description, active_form,
                        status, owner, metadata_json, created_at, updated_at
                    ) VALUES ($1,$2,$3,$4,$5,'pending',null,$6,$7,$8)
                    """,
                    session_id,
                    task_id,
                    input.subject,
                    input.description,
                    input.active_form,
                    json.dumps(input.metadata or {}),
                    now,
                    now,
                )
                await conn.execute(
                    "INSERT INTO task_counters (session_id, next_id) VALUES ($1, $2) "
                    "ON CONFLICT (session_id) DO UPDATE SET next_id = $2",
                    session_id,
                    next_id + 1,
                )
                await conn.execute(
                    "UPDATE sessions SET updated_at = $1 WHERE id = $2",
                    now,
                    session_id,
                )
                result = await _get_task_pg(conn, session_id, task_id)
        if result is None:
            raise KeyError(f"task not found after create: {task_id}")
        return result

    async def get_task(self, session_id: str, task_id: str) -> Task | None:
        pool = await self._ensure()
        async with pool.acquire() as conn:
            return await _get_task_pg(conn, session_id, task_id)

    async def list_tasks(self, session_id: str) -> list[Task]:
        pool = await self._ensure()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, subject, description, active_form, status, owner,
                       metadata_json, created_at, updated_at
                FROM tasks WHERE session_id = $1
                ORDER BY CAST(id AS INTEGER) ASC
                """,
                session_id,
            )
            out: list[Task] = []
            for row in rows:
                tid = str(row["id"])
                blocks, blocked_by = await _load_edges_pg(conn, session_id, tid)
                out.append(_row_to_task(session_id, row, blocks, blocked_by))
        return out

    async def update_task(self, session_id: str, task_id: str, patch: TaskPatch) -> Task | None:
        pool = await self._ensure()
        async with pool.acquire() as conn:
            async with conn.transaction():
                existing = await _get_task_pg(conn, session_id, task_id)
                if existing is None:
                    return None
                if patch.status == "deleted":
                    await _delete_task_pg(conn, session_id, task_id)
                    return None
                now = now_iso()
                if patch.subject is not None:
                    await conn.execute(
                        "UPDATE tasks SET subject=$1 WHERE session_id=$2 AND id=$3",
                        patch.subject,
                        session_id,
                        task_id,
                    )
                if patch.description is not None:
                    await conn.execute(
                        "UPDATE tasks SET description=$1 WHERE session_id=$2 AND id=$3",
                        patch.description,
                        session_id,
                        task_id,
                    )
                if patch.active_form is not None:
                    await conn.execute(
                        "UPDATE tasks SET active_form=$1 WHERE session_id=$2 AND id=$3",
                        patch.active_form,
                        session_id,
                        task_id,
                    )
                if patch.status is not None:
                    await conn.execute(
                        "UPDATE tasks SET status=$1 WHERE session_id=$2 AND id=$3",
                        patch.status,
                        session_id,
                        task_id,
                    )
                if patch.owner is not None:
                    await conn.execute(
                        "UPDATE tasks SET owner=$1 WHERE session_id=$2 AND id=$3",
                        patch.owner,
                        session_id,
                        task_id,
                    )
                if patch.metadata is not None:
                    md = dict(existing.metadata)
                    for k, v in patch.metadata.items():
                        if v is None:
                            md.pop(k, None)
                        else:
                            md[k] = v
                    await conn.execute(
                        "UPDATE tasks SET metadata_json=$1 WHERE session_id=$2 AND id=$3",
                        json.dumps(md),
                        session_id,
                        task_id,
                    )
                for to_id in patch.remove_blocks or []:
                    await conn.execute(
                        "DELETE FROM task_edges WHERE session_id=$1 "
                        "AND from_task_id=$2 AND to_task_id=$3",
                        session_id,
                        task_id,
                        to_id,
                    )
                for from_id in patch.remove_blocked_by or []:
                    await conn.execute(
                        "DELETE FROM task_edges WHERE session_id=$1 "
                        "AND from_task_id=$2 AND to_task_id=$3",
                        session_id,
                        from_id,
                        task_id,
                    )
                for to_id in patch.add_blocks or []:
                    await conn.execute(
                        "INSERT INTO task_edges "
                        "(session_id, from_task_id, to_task_id, kind) "
                        "VALUES ($1,$2,$3,'blocks') ON CONFLICT DO NOTHING",
                        session_id,
                        task_id,
                        to_id,
                    )
                for from_id in patch.add_blocked_by or []:
                    await conn.execute(
                        "INSERT INTO task_edges "
                        "(session_id, from_task_id, to_task_id, kind) "
                        "VALUES ($1,$2,$3,'blocks') ON CONFLICT DO NOTHING",
                        session_id,
                        from_id,
                        task_id,
                    )
                await conn.execute(
                    "UPDATE tasks SET updated_at=$1 WHERE session_id=$2 AND id=$3",
                    now,
                    session_id,
                    task_id,
                )
                await conn.execute(
                    "UPDATE sessions SET updated_at=$1 WHERE id=$2",
                    now,
                    session_id,
                )
                return await _get_task_pg(conn, session_id, task_id)

    async def delete_task(self, session_id: str, task_id: str) -> bool:
        pool = await self._ensure()
        async with pool.acquire() as conn:
            async with conn.transaction():
                return await _delete_task_pg(conn, session_id, task_id)

    async def claim_task(self, session_id: str, task_id: str, owner: str) -> Task | None:
        pool = await self._ensure()
        async with pool.acquire() as conn:
            async with conn.transaction():
                ts = now_iso()
                # Row-level conditional update: only one concurrent claimer wins.
                claimed = await conn.fetchrow(
                    """
                    UPDATE tasks SET owner=$1, updated_at=$2
                    WHERE session_id=$3 AND id=$4 AND owner IS NULL AND status='pending'
                    RETURNING id
                    """,
                    owner,
                    ts,
                    session_id,
                    task_id,
                )
                if claimed is None:
                    return None
                await conn.execute("UPDATE sessions SET updated_at=$1 WHERE id=$2", ts, session_id)
                return await _get_task_pg(conn, session_id, task_id)

    async def ready_tasks(self, session_id: str) -> list[Task]:
        tasks = await self.list_tasks(session_id)
        by_id = {t.id: t for t in tasks}
        return [
            t
            for t in tasks
            if t.status == "pending"
            and t.owner is None
            and all(
                by_id.get(b) is not None and by_id[b].status == "completed" for b in t.blocked_by
            )
        ]

    async def release_task(self, session_id: str, task_id: str) -> Task | None:
        pool = await self._ensure()
        async with pool.acquire() as conn:
            async with conn.transaction():
                task = await _get_task_pg(conn, session_id, task_id)
                if task is None:
                    return None
                if task.status == "completed":
                    return task  # don't resurrect a finished task
                now = now_iso()
                await conn.execute(
                    "UPDATE tasks SET owner=null, status='pending', updated_at=$1 "
                    "WHERE session_id=$2 AND id=$3",
                    now,
                    session_id,
                    task_id,
                )
                await conn.execute("UPDATE sessions SET updated_at=$1 WHERE id=$2", now, session_id)
                return await _get_task_pg(conn, session_id, task_id)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None


# ── Async PG helpers (run within an acquired connection) ────────────────────


def _record(row: Any) -> SessionRecord:
    return SessionRecord(
        id=row["id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        meta=json.loads(row["meta"]),
        invoked_skills=list(json.loads(row["invoked_skills"] or "[]")),
    )


def _row_to_task(
    session_id: str,
    row: Any,
    blocks: list[str],
    blocked_by: list[str],
) -> Task:
    return Task(
        id=str(row["id"]),
        session_id=session_id,
        subject=row["subject"],
        description=row["description"],
        active_form=row["active_form"],
        status=row["status"],  # type: ignore[arg-type]
        owner=row["owner"],
        blocks=blocks,
        blocked_by=blocked_by,
        metadata=dict(json.loads(row["metadata_json"] or "{}")),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def _load_edges_pg(conn: Any, session_id: str, task_id: str) -> tuple[list[str], list[str]]:
    blocks = [
        str(r["to_task_id"])
        for r in await conn.fetch(
            "SELECT to_task_id FROM task_edges WHERE session_id=$1 AND from_task_id=$2",
            session_id,
            task_id,
        )
    ]
    blocked_by = [
        str(r["from_task_id"])
        for r in await conn.fetch(
            "SELECT from_task_id FROM task_edges WHERE session_id=$1 AND to_task_id=$2",
            session_id,
            task_id,
        )
    ]
    return blocks, blocked_by


async def _get_task_pg(conn: Any, session_id: str, task_id: str) -> Task | None:
    row = await conn.fetchrow(
        """
        SELECT id, subject, description, active_form, status, owner,
               metadata_json, created_at, updated_at
        FROM tasks WHERE session_id = $1 AND id = $2
        """,
        session_id,
        task_id,
    )
    if row is None:
        return None
    blocks, blocked_by = await _load_edges_pg(conn, session_id, task_id)
    return _row_to_task(session_id, row, blocks, blocked_by)


async def _delete_task_pg(conn: Any, session_id: str, task_id: str) -> bool:
    await conn.execute(
        "DELETE FROM task_edges WHERE session_id=$1 AND (from_task_id=$2 OR to_task_id=$2)",
        session_id,
        task_id,
    )
    result = await conn.execute(
        "DELETE FROM tasks WHERE session_id=$1 AND id=$2",
        session_id,
        task_id,
    )
    await conn.execute(
        "UPDATE sessions SET updated_at=$1 WHERE id=$2",
        now_iso(),
        session_id,
    )
    # asyncpg DELETE returns "DELETE N" — extract count
    try:
        count = int(result.split()[-1])
    except (ValueError, IndexError, AttributeError):
        count = 0
    return count > 0
