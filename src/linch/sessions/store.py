from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from linch.sessions.tasks import CreateTaskInput, Task, TaskPatch
from linch.types import Message, message_from_dict, message_to_dict

# Snapshot wire version. Bumped only on a breaking shape change; readers are
# best-effort and ignore unknown future keys (mirrors run_store.SCHEMA_VERSION).
SNAPSHOT_SCHEMA_VERSION = 1


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


@dataclass(slots=True)
class ProviderViewSnapshot:
    """A durable compacted ``provider_view`` and the message watermark it covers.

    ``covers_seq`` is the highest stored message ``seq`` the compacted view
    accounts for; on reload the view is restored and messages with a greater
    ``seq`` are appended. Persisting this is an optional store capability
    (Phase 3.3) — it never mutates or replaces the append-only message history.
    """

    provider_view: list[Message]
    covers_seq: int


def snapshot_to_dict(snapshot: ProviderViewSnapshot) -> dict[str, Any]:
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "covers_seq": snapshot.covers_seq,
        "provider_view": [message_to_dict(message) for message in snapshot.provider_view],
    }


def snapshot_from_dict(raw: dict[str, Any]) -> ProviderViewSnapshot:
    # Version-tolerant: unknown future keys are ignored, missing keys default.
    return ProviderViewSnapshot(
        provider_view=[message_from_dict(message) for message in raw.get("provider_view", [])],
        covers_seq=int(raw.get("covers_seq", 0)),
    )


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


class ProviderViewSnapshotStore(Protocol):
    """Optional ``SessionStore`` capability: persist/restore a compacted view.

    Detected at runtime with ``getattr(store, "load_provider_snapshot", None)``;
    it is not part of the required ``SessionStore`` contract. A store that
    implements both methods lets a reloaded session restore its compacted
    ``provider_view`` instead of rebuilding it from the full message history.
    """

    async def save_provider_snapshot(self, id: str, snapshot: ProviderViewSnapshot) -> None: ...

    async def load_provider_snapshot(self, id: str) -> ProviderViewSnapshot | None: ...
