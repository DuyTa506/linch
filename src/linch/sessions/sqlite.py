from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from uuid import uuid4

from linch.sessions.memory import now_iso
from linch.sessions.tasks import CreateTaskInput, Task, TaskPatch
from linch.types import Message, message_from_dict, message_to_dict

from ..storage._executor import SqliteExecutor
from .store import SessionRecord, StoredMessage

# ── DDL ─────────────────────────────────────────────────────────────────────


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        create table if not exists sessions (
          id text primary key,
          created_at text not null,
          updated_at text not null,
          meta text not null,
          invoked_skills text not null default '[]'
        );
        create table if not exists messages (
          session_id text not null,
          seq integer not null,
          appended_at text not null,
          message text not null,
          primary key (session_id, seq)
        );
        create table if not exists task_counters (
          session_id text primary key,
          next_id integer not null
        );
        create table if not exists tasks (
          session_id text not null,
          id text not null,
          subject text not null,
          description text not null,
          active_form text,
          status text not null,
          owner text,
          metadata_json text not null,
          created_at text not null,
          updated_at text not null,
          primary key (session_id, id)
        );
        create table if not exists task_edges (
          session_id text not null,
          from_task_id text not null,
          to_task_id text not null,
          kind text not null default 'blocks',
          primary key (session_id, from_task_id, to_task_id)
        );
        """
    )


# ── Sync helpers (all run on the executor's worker thread) ───────────────────
# These functions take `conn` as their first argument and return plain Python
# objects. They are called exclusively from within SqliteExecutor.run() lambdas.


def _record(row: object) -> SessionRecord:
    return SessionRecord(
        id=row[0],  # type: ignore[index]
        created_at=row[1],  # type: ignore[index]
        updated_at=row[2],  # type: ignore[index]
        meta=json.loads(row[3]),  # type: ignore[index]
        invoked_skills=list(json.loads(row[4] or "[]")),  # type: ignore[index]
    )


def _load_edges(
    conn: sqlite3.Connection, session_id: str, task_id: str
) -> tuple[list[str], list[str]]:
    blocks = [
        row[0]
        for row in conn.execute(
            "select to_task_id from task_edges where session_id = ? and from_task_id = ?",
            (session_id, task_id),
        ).fetchall()
    ]
    blocked_by = [
        row[0]
        for row in conn.execute(
            "select from_task_id from task_edges where session_id = ? and to_task_id = ?",
            (session_id, task_id),
        ).fetchall()
    ]
    return blocks, blocked_by


def _row_to_task(
    session_id: str,
    row: object,
    blocks: list[str],
    blocked_by: list[str],
) -> Task:
    (
        task_id,
        subject,
        description,
        active_form,
        status,
        owner,
        metadata_json,
        created_at,
        updated_at,
    ) = row  # type: ignore[misc]
    return Task(
        id=task_id,
        session_id=session_id,
        subject=subject,
        description=description,
        active_form=active_form,
        status=status,  # type: ignore[arg-type]
        owner=owner,
        blocks=blocks,
        blocked_by=blocked_by,
        metadata=dict(json.loads(metadata_json or "{}")),
        created_at=created_at,
        updated_at=updated_at,
    )


def _get_task_sync(
    conn: sqlite3.Connection, session_id: str, task_id: str
) -> Task | None:
    row = conn.execute(
        """
        select id, subject, description, active_form, status, owner,
               metadata_json, created_at, updated_at
        from tasks where session_id = ? and id = ?
        """,
        (session_id, task_id),
    ).fetchone()
    if row is None:
        return None
    blocks, blocked_by = _load_edges(conn, session_id, task_id)
    return _row_to_task(session_id, row, blocks, blocked_by)


def _delete_task_sync(conn: sqlite3.Connection, session_id: str, task_id: str) -> bool:
    conn.execute(
        "delete from task_edges where session_id = ? and (from_task_id = ? or to_task_id = ?)",
        (session_id, task_id, task_id),
    )
    cur = conn.execute(
        "delete from tasks where session_id = ? and id = ?",
        (session_id, task_id),
    )
    conn.execute(
        "update sessions set updated_at = ? where id = ?",
        (now_iso(), session_id),
    )
    conn.commit()
    return bool(cur.rowcount)


# ── Store ────────────────────────────────────────────────────────────────────


class SqliteSessionStore:
    """Session store backed by SQLite.

    All database I/O runs on a single dedicated worker thread via
    :class:`~linch.storage._executor.SqliteExecutor`, so the asyncio event
    loop is never blocked by a ``commit()``/fsync.  The connection is pinned to
    that thread, which means no ``asyncio.Lock`` is needed and
    ``check_same_thread`` (default ``True``) remains as a free correctness
    assertion.

    Safe for concurrent access from multiple coroutines and subagents sharing
    the same store instance — operations are serialised through the one worker
    thread.
    """

    def __init__(self, path: str | Path = ".linch/sessions.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._exec = SqliteExecutor(self.path, init=_init_schema, wal=True)

    # ── SessionStore protocol ────────────────────────────────────────────────

    async def create(
        self, *, id: str | None = None, meta: dict[str, object] | None = None
    ) -> SessionRecord:
        _meta = meta or {}
        return await self._exec.run(lambda c: _create(c, id, _meta))

    async def load(self, id: str) -> SessionRecord | None:
        return await self._exec.run(lambda c: _load(c, id))

    async def load_messages(self, id: str) -> list[StoredMessage]:
        return await self._exec.run(lambda c: _load_messages(c, id))

    async def append_messages(self, id: str, messages: list[Message]) -> list[StoredMessage]:
        return await self._exec.run(lambda c: _append_messages(c, id, messages))

    async def update_meta(self, id: str, meta: dict[str, object]) -> SessionRecord:
        return await self._exec.run(lambda c: _update_meta(c, id, meta))

    async def set_invoked_skills(self, id: str, skills: list[dict[str, object]]) -> None:
        return await self._exec.run(lambda c: _set_invoked_skills(c, id, skills))

    async def list(
        self, *, limit: int | None = None, offset: int = 0
    ) -> list[SessionRecord]:
        return await self._exec.run(lambda c: _list(c, limit, offset))

    async def delete(self, id: str) -> None:
        await self._exec.run(lambda c: _delete(c, id))

    async def create_task(self, session_id: str, input: CreateTaskInput) -> Task:
        return await self._exec.run(lambda c: _create_task(c, session_id, input))

    async def get_task(self, session_id: str, task_id: str) -> Task | None:
        return await self._exec.run(
            lambda c: _get_task_sync(c, session_id, task_id)
        )

    async def list_tasks(self, session_id: str) -> list[Task]:
        return await self._exec.run(lambda c: _list_tasks(c, session_id))

    async def update_task(
        self, session_id: str, task_id: str, patch: TaskPatch
    ) -> Task | None:
        return await self._exec.run(
            lambda c: _update_task(c, session_id, task_id, patch)
        )

    async def delete_task(self, session_id: str, task_id: str) -> bool:
        return await self._exec.run(
            lambda c: _delete_task_sync(c, session_id, task_id)
        )

    async def close(self) -> None:
        await self._exec.close()

    def __enter__(self) -> SqliteSessionStore:
        return self

    def __exit__(self, *_: object) -> None:
        self._exec.close_sync()

    async def __aenter__(self) -> SqliteSessionStore:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()


# ── Sync implementations (run on the worker thread) ──────────────────────────


def _create(
    conn: sqlite3.Connection, id: str | None, meta: dict[str, object]
) -> SessionRecord:
    sid = id or str(uuid4())
    row = conn.execute(
        "select id, created_at, updated_at, meta, invoked_skills from sessions where id = ?",
        (sid,),
    ).fetchone()
    if row:
        return _record(row)
    ts = now_iso()
    conn.execute(
        "insert into sessions (id, created_at, updated_at, meta, invoked_skills) "
        "values (?, ?, ?, ?, ?)",
        (sid, ts, ts, json.dumps(meta), "[]"),
    )
    conn.execute(
        "insert or ignore into task_counters (session_id, next_id) values (?, 1)",
        (sid,),
    )
    conn.commit()
    return SessionRecord(id=sid, created_at=ts, updated_at=ts, meta=meta)


def _load(conn: sqlite3.Connection, id: str) -> SessionRecord | None:
    row = conn.execute(
        "select id, created_at, updated_at, meta, invoked_skills from sessions where id = ?",
        (id,),
    ).fetchone()
    return _record(row) if row else None


def _load_messages(conn: sqlite3.Connection, id: str) -> list[StoredMessage]:
    rows = conn.execute(
        "select seq, appended_at, message from messages where session_id = ? order by seq",
        (id,),
    ).fetchall()
    return [
        StoredMessage(
            seq=row[0],
            appended_at=row[1],
            message=message_from_dict(json.loads(row[2])),
        )
        for row in rows
    ]


def _append_messages(
    conn: sqlite3.Connection, id: str, messages: list[Message]
) -> list[StoredMessage]:
    ts = now_iso()
    cur_seq: int = conn.execute(
        "select coalesce(max(seq), 0) from messages where session_id = ?", (id,)
    ).fetchone()[0]
    stored: list[StoredMessage] = []
    for message in messages:
        cur_seq += 1
        conn.execute(
            "insert into messages (session_id, seq, appended_at, message) values (?, ?, ?, ?)",
            (id, cur_seq, ts, json.dumps(message_to_dict(message))),
        )
        stored.append(StoredMessage(seq=cur_seq, appended_at=ts, message=message))
    conn.execute("update sessions set updated_at = ? where id = ?", (ts, id))
    conn.commit()
    return stored


def _update_meta(
    conn: sqlite3.Connection, id: str, meta: dict[str, object]
) -> SessionRecord:
    row = conn.execute(
        "select id, created_at, updated_at, meta, invoked_skills from sessions where id = ?",
        (id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"session not found: {id}")
    existing = json.loads(row[3])
    existing.update(meta)
    ts = now_iso()
    conn.execute(
        "update sessions set updated_at = ?, meta = ? where id = ?",
        (ts, json.dumps(existing), id),
    )
    conn.commit()
    return SessionRecord(
        id=row[0],
        created_at=row[1],
        updated_at=ts,
        meta=existing,
        invoked_skills=list(json.loads(row[4] or "[]")),
    )


def _set_invoked_skills(
    conn: sqlite3.Connection, id: str, skills: list[dict[str, object]]
) -> None:
    now = now_iso()
    conn.execute(
        "update sessions set invoked_skills = ?, updated_at = ? where id = ?",
        (json.dumps(skills), now, id),
    )
    conn.commit()


def _list(
    conn: sqlite3.Connection, limit: int | None, offset: int
) -> list[SessionRecord]:
    sql = (
        "select id, created_at, updated_at, meta, invoked_skills "
        "from sessions order by updated_at desc"
    )
    args: list[object] = []
    if limit is not None:
        sql += " limit ? offset ?"
        args.extend([limit, offset])
    rows = conn.execute(sql, args).fetchall()
    return [_record(row) for row in rows]


def _delete(conn: sqlite3.Connection, id: str) -> None:
    conn.execute("delete from task_edges where session_id = ?", (id,))
    conn.execute("delete from tasks where session_id = ?", (id,))
    conn.execute("delete from task_counters where session_id = ?", (id,))
    conn.execute("delete from messages where session_id = ?", (id,))
    conn.execute("delete from sessions where id = ?", (id,))
    conn.commit()


def _create_task(
    conn: sqlite3.Connection, session_id: str, input: CreateTaskInput
) -> Task:
    row = conn.execute(
        "select next_id from task_counters where session_id = ?", (session_id,)
    ).fetchone()
    next_id = int(row[0]) if row else 1
    task_id = str(next_id)
    now = now_iso()
    conn.execute(
        """
        insert into tasks (
          session_id, id, subject, description, active_form,
          status, owner, metadata_json, created_at, updated_at
        ) values (?, ?, ?, ?, ?, 'pending', null, ?, ?, ?)
        """,
        (
            session_id,
            task_id,
            input.subject,
            input.description,
            input.active_form,
            json.dumps(input.metadata or {}),
            now,
            now,
        ),
    )
    conn.execute(
        "insert or replace into task_counters (session_id, next_id) values (?, ?)",
        (session_id, next_id + 1),
    )
    conn.execute("update sessions set updated_at = ? where id = ?", (now, session_id))
    conn.commit()
    # Read back in the same executor job (atomic + no extra round trip)
    result = _get_task_sync(conn, session_id, task_id)
    if result is None:
        raise KeyError(f"task not found after create: {task_id}")
    return result


def _list_tasks(conn: sqlite3.Connection, session_id: str) -> list[Task]:
    rows = conn.execute(
        """
        select id, subject, description, active_form, status, owner,
               metadata_json, created_at, updated_at
        from tasks where session_id = ? order by cast(id as integer) asc
        """,
        (session_id,),
    ).fetchall()
    out: list[Task] = []
    for row in rows:
        task_id = str(row[0])
        blocks, blocked_by = _load_edges(conn, session_id, task_id)
        out.append(_row_to_task(session_id, row, blocks, blocked_by))
    return out


def _update_task(
    conn: sqlite3.Connection, session_id: str, task_id: str, patch: TaskPatch
) -> Task | None:
    existing = _get_task_sync(conn, session_id, task_id)
    if existing is None:
        return None
    if patch.status == "deleted":
        _delete_task_sync(conn, session_id, task_id)
        return None

    now = now_iso()
    if patch.subject is not None:
        conn.execute(
            "update tasks set subject = ? where session_id = ? and id = ?",
            (patch.subject, session_id, task_id),
        )
    if patch.description is not None:
        conn.execute(
            "update tasks set description = ? where session_id = ? and id = ?",
            (patch.description, session_id, task_id),
        )
    if patch.active_form is not None:
        conn.execute(
            "update tasks set active_form = ? where session_id = ? and id = ?",
            (patch.active_form, session_id, task_id),
        )
    if patch.status is not None:
        conn.execute(
            "update tasks set status = ? where session_id = ? and id = ?",
            (patch.status, session_id, task_id),
        )
    if patch.owner is not None:
        conn.execute(
            "update tasks set owner = ? where session_id = ? and id = ?",
            (patch.owner, session_id, task_id),
        )
    if patch.metadata is not None:
        metadata = dict(existing.metadata)
        for key, value in patch.metadata.items():
            if value is None:
                metadata.pop(key, None)
            else:
                metadata[key] = value
        conn.execute(
            "update tasks set metadata_json = ? where session_id = ? and id = ?",
            (json.dumps(metadata), session_id, task_id),
        )

    _delete_edge = (
        "delete from task_edges "
        "where session_id = ? and from_task_id = ? and to_task_id = ?"
    )
    _insert_edge = (
        "insert or ignore into task_edges (session_id, from_task_id, to_task_id, kind) "
        "values (?, ?, ?, 'blocks')"
    )
    for to_id in patch.remove_blocks or []:
        conn.execute(_delete_edge, (session_id, task_id, to_id))
    for from_id in patch.remove_blocked_by or []:
        conn.execute(_delete_edge, (session_id, from_id, task_id))
    for to_id in patch.add_blocks or []:
        conn.execute(_insert_edge, (session_id, task_id, to_id))
    for from_id in patch.add_blocked_by or []:
        conn.execute(_insert_edge, (session_id, from_id, task_id))

    conn.execute(
        "update tasks set updated_at = ? where session_id = ? and id = ?",
        (now, session_id, task_id),
    )
    conn.execute(
        "update sessions set updated_at = ? where id = ?",
        (now, session_id),
    )
    conn.commit()
    return _get_task_sync(conn, session_id, task_id)
