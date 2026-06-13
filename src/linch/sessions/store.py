from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from linch.sessions.tasks import CreateTaskInput, Task, TaskPatch
from linch.types import Message


@dataclass(slots=True)
class SessionRecord:
    id: str
    created_at: str
    updated_at: str
    meta: dict[str, object] = field(default_factory=dict)
    invoked_skills: list[dict[str, object]] = field(default_factory=list)


@dataclass(slots=True)
class StoredMessage:
    seq: int
    appended_at: str
    message: Message


class SessionStore(Protocol):
    async def create(
        self,
        *,
        id: str | None = None,
        meta: dict[str, object] | None = None,
    ) -> SessionRecord: ...

    async def load(self, id: str) -> SessionRecord | None: ...

    async def load_messages(self, id: str) -> list[StoredMessage]: ...

    async def append_messages(self, id: str, messages: list[Message]) -> list[StoredMessage]: ...

    async def update_meta(self, id: str, meta: dict[str, object]) -> SessionRecord: ...

    async def set_invoked_skills(self, id: str, skills: list[dict[str, object]]) -> None: ...

    async def list(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[SessionRecord]: ...

    async def delete(self, id: str) -> None: ...

    async def create_task(self, session_id: str, input: CreateTaskInput) -> Task: ...

    async def get_task(self, session_id: str, task_id: str) -> Task | None: ...

    async def list_tasks(self, session_id: str) -> list[Task]: ...

    async def update_task(
        self,
        session_id: str,
        task_id: str,
        patch: TaskPatch,
    ) -> Task | None: ...

    async def delete_task(self, session_id: str, task_id: str) -> bool: ...

    async def claim_task(self, session_id: str, task_id: str, owner: str) -> Task | None: ...

    async def ready_tasks(self, session_id: str) -> list[Task]: ...

    async def release_task(self, session_id: str, task_id: str) -> Task | None: ...

    async def close(self) -> None: ...
