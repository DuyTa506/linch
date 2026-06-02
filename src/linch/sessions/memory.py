from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from linch.sessions.tasks import CreateTaskInput, Task, TaskPatch
from linch.types import Message

from .store import SessionRecord, StoredMessage


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class InMemorySessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, SessionRecord] = {}
        self._messages: dict[str, list[StoredMessage]] = {}
        self._tasks: dict[str, dict[str, Task]] = {}
        self._task_counter: dict[str, int] = {}

    async def create(
        self, *, id: str | None = None, meta: dict[str, object] | None = None
    ) -> SessionRecord:
        sid = id or str(uuid4())
        existing = self._sessions.get(sid)
        if existing is not None:
            return existing
        ts = now_iso()
        record = SessionRecord(id=sid, created_at=ts, updated_at=ts, meta=dict(meta or {}))
        self._sessions[sid] = record
        self._messages[sid] = []
        self._tasks[sid] = {}
        self._task_counter[sid] = 1
        return record

    async def load(self, id: str) -> SessionRecord | None:
        return self._sessions.get(id)

    async def load_messages(self, id: str) -> list[StoredMessage]:
        return list(self._messages.get(id, []))

    async def append_messages(self, id: str, messages: list[Message]) -> list[StoredMessage]:
        if id not in self._sessions:
            raise KeyError(f"session not found: {id}")
        ts = now_iso()
        bucket = self._messages[id]
        stored: list[StoredMessage] = []
        for message in messages:
            row = StoredMessage(seq=len(bucket) + 1, appended_at=ts, message=message)
            bucket.append(row)
            stored.append(row)
        rec = self._sessions[id]
        rec.updated_at = ts
        return stored

    async def update_meta(self, id: str, meta: dict[str, object]) -> SessionRecord:
        rec = self._sessions[id]
        rec.meta.update(meta)
        rec.updated_at = now_iso()
        return rec

    async def set_invoked_skills(self, id: str, skills: list[dict[str, object]]) -> None:
        rec = self._sessions[id]
        rec.invoked_skills = list(skills)
        rec.updated_at = now_iso()

    async def list(self, *, limit: int | None = None, offset: int = 0) -> list[SessionRecord]:
        rows = sorted(self._sessions.values(), key=lambda rec: rec.updated_at, reverse=True)
        if offset:
            rows = rows[offset:]
        if limit is not None:
            rows = rows[:limit]
        return rows

    async def delete(self, id: str) -> None:
        self._sessions.pop(id, None)
        self._messages.pop(id, None)
        self._tasks.pop(id, None)
        self._task_counter.pop(id, None)

    async def create_task(self, session_id: str, input: CreateTaskInput) -> Task:
        if session_id not in self._sessions:
            raise KeyError(f"session not found: {session_id}")
        task_id = str(self._task_counter.get(session_id, 1))
        self._task_counter[session_id] = int(task_id) + 1
        ts = now_iso()
        task = Task(
            id=task_id,
            session_id=session_id,
            subject=input.subject,
            description=input.description,
            active_form=input.active_form,
            status="pending",
            metadata=dict(input.metadata or {}),
            created_at=ts,
            updated_at=ts,
        )
        self._tasks.setdefault(session_id, {})[task_id] = task
        self._sessions[session_id].updated_at = ts
        return task

    async def get_task(self, session_id: str, task_id: str) -> Task | None:
        return self._tasks.get(session_id, {}).get(task_id)

    async def list_tasks(self, session_id: str) -> list[Task]:
        tasks = self._tasks.get(session_id, {})
        return sorted(tasks.values(), key=lambda t: int(t.id))

    async def update_task(self, session_id: str, task_id: str, patch: TaskPatch) -> Task | None:
        task = self._tasks.get(session_id, {}).get(task_id)
        if task is None:
            return None
        if patch.status == "deleted":
            await self.delete_task(session_id, task_id)
            return None

        if patch.subject is not None:
            task.subject = patch.subject
        if patch.description is not None:
            task.description = patch.description
        if patch.active_form is not None:
            task.active_form = patch.active_form
        if patch.status is not None and patch.status != "deleted":
            task.status = patch.status
        if patch.owner is not None:
            task.owner = patch.owner
        if patch.metadata is not None:
            for key, value in patch.metadata.items():
                if value is None:
                    task.metadata.pop(key, None)
                else:
                    task.metadata[key] = value

        if patch.add_blocks:
            for tid in patch.add_blocks:
                if tid not in task.blocks:
                    task.blocks.append(tid)
        if patch.remove_blocks:
            task.blocks = [tid for tid in task.blocks if tid not in patch.remove_blocks]

        if patch.add_blocked_by:
            for tid in patch.add_blocked_by:
                if tid not in task.blocked_by:
                    task.blocked_by.append(tid)
        if patch.remove_blocked_by:
            task.blocked_by = [tid for tid in task.blocked_by if tid not in patch.remove_blocked_by]

        task.updated_at = now_iso()
        self._sessions[session_id].updated_at = task.updated_at
        return task

    async def delete_task(self, session_id: str, task_id: str) -> bool:
        bucket = self._tasks.get(session_id, {})
        existed = task_id in bucket
        bucket.pop(task_id, None)
        if existed:
            for task in bucket.values():
                task.blocks = [tid for tid in task.blocks if tid != task_id]
                task.blocked_by = [tid for tid in task.blocked_by if tid != task_id]
            self._sessions[session_id].updated_at = now_iso()
        return existed

    async def close(self) -> None:
        return None
