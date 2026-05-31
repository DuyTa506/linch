from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from uuid import uuid4

from agent_kit.sessions.memory import now_iso
from agent_kit.sessions.tasks import CreateTaskInput, Task, TaskPatch
from agent_kit.types import Message, message_from_dict, message_to_dict

from .store import SessionRecord, StoredMessage


class SqliteSessionStore:
    def __init__(self, path: str | Path = ".agent_kit/sessions.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute("pragma journal_mode=wal")
        self._conn.executescript(
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
        self._conn.commit()

    async def create(
        self, *, id: str | None = None, meta: dict[str, object] | None = None
    ) -> SessionRecord:
        return self._create(id, meta or {})

    def _create(self, id: str | None, meta: dict[str, object]) -> SessionRecord:
        sid = id or str(uuid4())
        row = self._conn.execute(
            "select id, created_at, updated_at, meta, invoked_skills from sessions where id = ?",
            (sid,),
        ).fetchone()
        if row:
            return self._record(row)
        ts = now_iso()
        self._conn.execute(
            (
                "insert into sessions "
                "(id, created_at, updated_at, meta, invoked_skills) "
                "values (?, ?, ?, ?, ?)"
            ),
            (sid, ts, ts, json.dumps(meta), "[]"),
        )
        self._conn.execute(
            "insert or ignore into task_counters (session_id, next_id) values (?, 1)",
            (sid,),
        )
        self._conn.commit()
        return SessionRecord(id=sid, created_at=ts, updated_at=ts, meta=meta)

    async def load(self, id: str) -> SessionRecord | None:
        return self._load(id)

    def _load(self, id: str) -> SessionRecord | None:
        row = self._conn.execute(
            "select id, created_at, updated_at, meta, invoked_skills from sessions where id = ?",
            (id,),
        ).fetchone()
        return self._record(row) if row else None

    async def load_messages(self, id: str) -> list[StoredMessage]:
        return self._load_messages(id)

    def _load_messages(self, id: str) -> list[StoredMessage]:
        rows = self._conn.execute(
            ("select seq, appended_at, message from messages where session_id = ? order by seq"),
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

    async def append_messages(self, id: str, messages: list[Message]) -> list[StoredMessage]:
        return self._append_messages(id, messages)

    def _append_messages(self, id: str, messages: list[Message]) -> list[StoredMessage]:
        ts = now_iso()
        cur = self._conn.execute(
            "select coalesce(max(seq), 0) from messages where session_id = ?", (id,)
        ).fetchone()[0]
        stored: list[StoredMessage] = []
        for message in messages:
            cur += 1
            self._conn.execute(
                "insert into messages (session_id, seq, appended_at, message) values (?, ?, ?, ?)",
                (id, cur, ts, json.dumps(message_to_dict(message))),
            )
            stored.append(StoredMessage(seq=cur, appended_at=ts, message=message))
        self._conn.execute("update sessions set updated_at = ? where id = ?", (ts, id))
        self._conn.commit()
        return stored

    async def update_meta(self, id: str, meta: dict[str, object]) -> SessionRecord:
        return self._update_meta(id, meta)

    def _update_meta(self, id: str, meta: dict[str, object]) -> SessionRecord:
        rec = self._load(id)
        if rec is None:
            raise KeyError(f"session not found: {id}")
        rec.meta.update(meta)
        rec.updated_at = now_iso()
        self._conn.execute(
            "update sessions set updated_at = ?, meta = ? where id = ?",
            (rec.updated_at, json.dumps(rec.meta), id),
        )
        self._conn.commit()
        return rec

    async def set_invoked_skills(self, id: str, skills: list[dict[str, object]]) -> None:
        now = now_iso()
        self._conn.execute(
            "update sessions set invoked_skills = ?, updated_at = ? where id = ?",
            (json.dumps(skills), now, id),
        )
        self._conn.commit()

    async def list(self, *, limit: int | None = None, offset: int = 0) -> list[SessionRecord]:
        return self._list(limit, offset)

    def _list(self, limit: int | None, offset: int) -> list[SessionRecord]:
        sql = (
            "select id, created_at, updated_at, meta, invoked_skills "
            "from sessions order by updated_at desc"
        )
        args: list[int] = []
        if limit is not None:
            sql += " limit ? offset ?"
            args.extend([limit, offset])
        rows = self._conn.execute(sql, args).fetchall()
        return [self._record(row) for row in rows]

    async def delete(self, id: str) -> None:
        self._delete(id)

    def _delete(self, id: str) -> None:
        self._conn.execute("delete from task_edges where session_id = ?", (id,))
        self._conn.execute("delete from tasks where session_id = ?", (id,))
        self._conn.execute("delete from task_counters where session_id = ?", (id,))
        self._conn.execute("delete from messages where session_id = ?", (id,))
        self._conn.execute("delete from sessions where id = ?", (id,))
        self._conn.commit()

    async def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _record(row: tuple[str, str, str, str, str]) -> SessionRecord:
        return SessionRecord(
            id=row[0],
            created_at=row[1],
            updated_at=row[2],
            meta=json.loads(row[3]),
            invoked_skills=list(json.loads(row[4] or "[]")),
        )

    def _load_edges(self, session_id: str, task_id: str) -> tuple[list[str], list[str]]:
        blocks = [
            row[0]
            for row in self._conn.execute(
                "select to_task_id from task_edges where session_id = ? and from_task_id = ?",
                (session_id, task_id),
            ).fetchall()
        ]
        blocked_by = [
            row[0]
            for row in self._conn.execute(
                "select from_task_id from task_edges where session_id = ? and to_task_id = ?",
                (session_id, task_id),
            ).fetchall()
        ]
        return blocks, blocked_by

    @staticmethod
    def _row_to_task(
        session_id: str,
        row: tuple[
            str,
            str,
            str,
            str | None,
            str,
            str | None,
            str,
            str,
            str,
        ],
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
        ) = row
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

    async def create_task(self, session_id: str, input: CreateTaskInput) -> Task:
        row = self._conn.execute(
            "select next_id from task_counters where session_id = ?", (session_id,)
        ).fetchone()
        next_id = int(row[0]) if row else 1
        task_id = str(next_id)
        now = now_iso()
        self._conn.execute(
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
        self._conn.execute(
            "insert or replace into task_counters (session_id, next_id) values (?, ?)",
            (session_id, next_id + 1),
        )
        self._conn.execute("update sessions set updated_at = ? where id = ?", (now, session_id))
        self._conn.commit()
        task = await self.get_task(session_id, task_id)
        if task is None:
            raise KeyError(f"task not found after create: {task_id}")
        return task

    async def get_task(self, session_id: str, task_id: str) -> Task | None:
        row = self._conn.execute(
            """
            select id, subject, description, active_form, status, owner,
                   metadata_json, created_at, updated_at
            from tasks where session_id = ? and id = ?
            """,
            (session_id, task_id),
        ).fetchone()
        if row is None:
            return None
        blocks, blocked_by = self._load_edges(session_id, task_id)
        return self._row_to_task(session_id, row, blocks, blocked_by)

    async def list_tasks(self, session_id: str) -> list[Task]:
        rows = self._conn.execute(
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
            blocks, blocked_by = self._load_edges(session_id, task_id)
            out.append(self._row_to_task(session_id, row, blocks, blocked_by))
        return out

    async def update_task(self, session_id: str, task_id: str, patch: TaskPatch) -> Task | None:
        existing = await self.get_task(session_id, task_id)
        if existing is None:
            return None
        if patch.status == "deleted":
            await self.delete_task(session_id, task_id)
            return None

        now = now_iso()
        if patch.subject is not None:
            self._conn.execute(
                "update tasks set subject = ? where session_id = ? and id = ?",
                (patch.subject, session_id, task_id),
            )
        if patch.description is not None:
            self._conn.execute(
                "update tasks set description = ? where session_id = ? and id = ?",
                (patch.description, session_id, task_id),
            )
        if patch.active_form is not None:
            self._conn.execute(
                "update tasks set active_form = ? where session_id = ? and id = ?",
                (patch.active_form, session_id, task_id),
            )
        if patch.status is not None:
            self._conn.execute(
                "update tasks set status = ? where session_id = ? and id = ?",
                (patch.status, session_id, task_id),
            )
        if patch.owner is not None:
            self._conn.execute(
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
            self._conn.execute(
                "update tasks set metadata_json = ? where session_id = ? and id = ?",
                (json.dumps(metadata), session_id, task_id),
            )

        delete_edge = (
            "delete from task_edges where session_id = ? and from_task_id = ? and to_task_id = ?"
        )
        insert_edge = """
            insert or ignore into task_edges (session_id, from_task_id, to_task_id, kind)
            values (?, ?, ?, 'blocks')
        """
        for to_id in patch.remove_blocks or []:
            self._conn.execute(delete_edge, (session_id, task_id, to_id))
        for from_id in patch.remove_blocked_by or []:
            self._conn.execute(delete_edge, (session_id, from_id, task_id))
        for to_id in patch.add_blocks or []:
            self._conn.execute(insert_edge, (session_id, task_id, to_id))
        for from_id in patch.add_blocked_by or []:
            self._conn.execute(insert_edge, (session_id, from_id, task_id))

        self._conn.execute(
            "update tasks set updated_at = ? where session_id = ? and id = ?",
            (now, session_id, task_id),
        )
        self._conn.execute(
            "update sessions set updated_at = ? where id = ?",
            (now, session_id),
        )
        self._conn.commit()
        return await self.get_task(session_id, task_id)

    async def delete_task(self, session_id: str, task_id: str) -> bool:
        self._conn.execute(
            "delete from task_edges where session_id = ? and (from_task_id = ? or to_task_id = ?)",
            (session_id, task_id, task_id),
        )
        cur = self._conn.execute(
            "delete from tasks where session_id = ? and id = ?",
            (session_id, task_id),
        )
        self._conn.execute(
            "update sessions set updated_at = ? where id = ?",
            (now_iso(), session_id),
        )
        self._conn.commit()
        return bool(cur.rowcount)
