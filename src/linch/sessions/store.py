from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from linch._wire import WIRE_VERSION, require_wire_version
from linch.durability import InboxDelivery
from linch.sessions.tasks import CreateTaskInput, Task, TaskPatch
from linch.types import Message, message_from_dict, message_to_dict

# Retain the existing name for private store callers; the shared wire module
# owns the single persisted-data version.
SNAPSHOT_SCHEMA_VERSION = WIRE_VERSION


@dataclass(slots=True)
class SessionRecord:
    """Describe the durable metadata for one conversation session.

    Args:
        id: Stable session identifier.
        created_at: Creation timestamp in ISO format.
        updated_at: Most recent session mutation timestamp in ISO format.
        meta: Application-defined session metadata.
        invoked_skills: Skill records persisted for this session.

    Attributes:
        id: Stable session identifier.
        created_at: Creation timestamp in ISO format.
        updated_at: Most recent session mutation timestamp in ISO format.
        meta: Application-defined session metadata.
        invoked_skills: Skill records persisted for this session.
    """

    id: str
    created_at: str
    updated_at: str
    meta: dict[str, object] = field(default_factory=dict)
    invoked_skills: list[dict[str, object]] = field(default_factory=list)


@dataclass(slots=True)
class StoredMessage:
    """Pair one persisted message with its session sequence and timestamp.

    Args:
        seq: Monotonically increasing sequence number within the session.
        appended_at: Timestamp when the message was stored, in ISO format.
        message: Stored conversation message.

    Attributes:
        seq: Monotonically increasing sequence number within the session.
        appended_at: Timestamp when the message was stored, in ISO format.
        message: Stored conversation message.
    """

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

    Args:
        provider_view: Compacted model-visible messages.
        covers_seq: Highest stored message sequence represented by the view.
        projection_id: Optional identity for the projection recipe or revision.

    Attributes:
        provider_view: Compacted model-visible messages.
        covers_seq: Highest stored message sequence represented by the view.
        projection_id: Optional identity for the projection recipe or revision.
    """

    provider_view: list[Message]
    covers_seq: int
    projection_id: str | None = None


def snapshot_to_dict(snapshot: ProviderViewSnapshot) -> dict[str, Any]:
    """Encode a provider-view snapshot into its current wire representation.

    Args:
        snapshot: Snapshot to encode.

    Returns:
        JSON-compatible snapshot data including the current wire version.
    """

    return {
        **({"projection_id": snapshot.projection_id} if snapshot.projection_id is not None else {}),
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "covers_seq": snapshot.covers_seq,
        "provider_view": [message_to_dict(message) for message in snapshot.provider_view],
    }


def snapshot_from_dict(raw: dict[str, Any]) -> ProviderViewSnapshot:
    """Decode and validate a provider-view snapshot from wire data.

    Args:
        raw: Decoded snapshot mapping to validate and convert.

    Returns:
        Provider-view snapshot reconstructed from the wire representation.

    Raises:
        ValueError: If the projection identity is present but invalid.
    """

    if not isinstance(raw, dict):
        require_wire_version(None)
    require_wire_version(raw.get("schema_version"))
    projection_id = raw.get("projection_id")
    if projection_id is not None and (
        not isinstance(projection_id, str) or not projection_id.strip()
    ):
        raise ValueError("invalid provider snapshot projection identity")
    return ProviderViewSnapshot(
        projection_id=projection_id,
        provider_view=[message_from_dict(message) for message in raw.get("provider_view", [])],
        covers_seq=int(raw.get("covers_seq", 0)),
    )


class SessionStore(Protocol):
    """Define the async persistence contract for session state.

    Implementations persist each successful mutation before returning. Message
    sequences are ordered per session; explicit session creation is idempotent,
    while task claims and inbox delivery provide their own atomicity rules.
    Multi-message appends are all-or-nothing. Inputs and returned session,
    message, and task graphs are detached from stored state.
    """

    async def create(
        self,
        *,
        id: str | None = None,
        meta: dict[str, object] | None = None,
    ) -> SessionRecord:
        """Create a session or return the existing record for an explicit ID.

        Args:
            id: Optional stable ID; omitted IDs are generated by the store.
            meta: Initial application-defined session metadata.

        Returns:
            The created or already-existing session record.
        """

        ...

    async def load(self, id: str) -> SessionRecord | None:
        """Load one session record by ID.

        Args:
            id: Session identifier to look up.

        Returns:
            The session record, or ``None`` when the ID is unknown.
        """

        ...

    async def load_messages(self, id: str) -> list[StoredMessage]:
        """Load a session's stored messages in ascending sequence order.

        Args:
            id: Session identifier whose messages should be loaded.

        Returns:
            Stored messages ordered by their per-session sequence number.
        """

        ...

    async def append_messages(self, id: str, messages: list[Message]) -> list[StoredMessage]:
        """Atomically append messages in order and assign consecutive session sequences.

        Args:
            id: Session identifier receiving the messages.
            messages: Messages to append in their intended provider order.

        Returns:
            Stored rows for the appended messages in the same order.

        Raises:
            KeyError: If the session does not exist.
        """

        ...

    async def update_meta(self, id: str, meta: dict[str, object]) -> SessionRecord:
        """Merge metadata into a session and persist the updated record.

        Args:
            id: Session identifier whose metadata should be updated.
            meta: Metadata keys and values to merge into the existing mapping.

        Returns:
            The updated session record.

        Raises:
            KeyError: If the session does not exist.
        """

        ...

    async def set_invoked_skills(self, id: str, skills: list[dict[str, object]]) -> None:
        """Replace the session's persisted invoked-skill records.

        Args:
            id: Session identifier whose skill records should be replaced.
            skills: Skill records to persist for the session.

        Raises:
            KeyError: If the session does not exist.
        """

        ...

    async def list(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[SessionRecord]:
        """List session records newest-updated first.

        Args:
            limit: Maximum number of records to return, or ``None`` for all.
            offset: Number of newest records to skip.

        Returns:
            Session records ordered by descending update timestamp.
        """

        ...

    async def delete(self, id: str) -> None:
        """Delete a session and its persisted messages and related state.

        Args:
            id: Session identifier to delete.

        Deleting an unknown ID is an idempotent no-op.
        """

        ...

    async def create_task(self, session_id: str, input: CreateTaskInput) -> Task:
        """Create a pending task with a session-local numeric ID.

        Args:
            session_id: Session that owns the task.
            input: Task fields to persist.

        Returns:
            The newly created task.

        Raises:
            KeyError: If the session does not exist.
        """

        ...

    async def get_task(self, session_id: str, task_id: str) -> Task | None:
        """Load one task by its session-local ID.

        Args:
            session_id: Session that owns the task.
            task_id: Task identifier to look up.

        Returns:
            The task, or ``None`` when it does not exist.
        """

        ...

    async def list_tasks(self, session_id: str) -> list[Task]:
        """List a session's tasks in ascending numeric task-ID order.

        Args:
            session_id: Session whose tasks should be listed.

        Returns:
            The session's tasks in deterministic task-ID order.
        """

        ...

    async def update_task(
        self,
        session_id: str,
        task_id: str,
        patch: TaskPatch,
    ) -> Task | None:
        """Apply a partial task update and dependency-edge changes.

        Args:
            session_id: Session that owns the task.
            task_id: Task identifier to update.
            patch: Fields and dependency edges to change.

        Returns:
            The updated task, or ``None`` when it does not exist or is deleted.
        """

        ...

    async def delete_task(self, session_id: str, task_id: str) -> bool:
        """Delete a task and remove its dependency edges.

        Args:
            session_id: Session that owns the task.
            task_id: Task identifier to delete.

        Returns:
            ``True`` when a task was deleted, otherwise ``False``.
        """

        ...

    async def claim_task(self, session_id: str, task_id: str, owner: str) -> Task | None:
        """Atomically claim one pending, unowned task for an owner.

        Args:
            session_id: Session that owns the task.
            task_id: Task identifier to claim.
            owner: Owner identity to record on a successful claim.

        Returns:
            The claimed task, or ``None`` when another owner already won or the
            task is not pending.
        """

        ...

    async def ready_tasks(self, session_id: str) -> list[Task]:
        """List pending, unowned tasks whose dependencies are complete.

        Args:
            session_id: Session whose ready tasks should be listed.

        Returns:
            Ready tasks in ascending numeric task-ID order.
        """

        ...

    async def release_task(self, session_id: str, task_id: str) -> Task | None:
        """Release a task owner and return unfinished tasks to pending state.

        Args:
            session_id: Session that owns the task.
            task_id: Task identifier to release.

        Returns:
            The released task, or ``None`` when it does not exist. Completed
            tasks are returned unchanged.
        """

        ...

    async def close(self) -> None:
        """Close store-owned resources and make no further calls through them."""

        ...


class ProviderViewSnapshotStore(Protocol):
    """Optional ``SessionStore`` capability: persist/restore a compacted view.

    Detected at runtime with ``getattr(store, "load_provider_snapshot", None)``;
    it is not part of the required ``SessionStore`` contract. A store that
    implements both methods lets a reloaded session restore its compacted
    ``provider_view`` instead of rebuilding it from the full message history.
    """

    async def save_provider_snapshot(self, id: str, snapshot: ProviderViewSnapshot) -> None:
        """Persist the latest compacted provider-view snapshot.

        Args:
            id: Session identifier owning the snapshot.
            snapshot: Compacted provider view and message watermark to store.
        """

        ...

    async def load_provider_snapshot(self, id: str) -> ProviderViewSnapshot | None:
        """Load a previously persisted provider-view snapshot.

        Args:
            id: Session identifier whose snapshot should be loaded.

        Returns:
            The stored snapshot, or ``None`` when no snapshot exists.
        """

        ...


class SessionInboxStore(Protocol):
    """Define optional exactly-once insertion for next-turn messages.

    Enqueue is idempotent for a delivery ID with the same digest. Commit
    inserts pending deliveries in enqueue order and advances the session
    message sequence before returning the resulting rows.
    """

    async def enqueue_inbox(self, id: str, delivery: InboxDelivery) -> None:
        """Durably enqueue one deduplicated inbox delivery.

        Args:
            id: Session identifier receiving the delivery.
            delivery: Delivery receipt and message payload to enqueue.

        Raises:
            KeyError: If the session does not exist.
            ConfigError: If the delivery ID already has a different digest.
        """

        ...

    async def commit_inbox(self, id: str, *, after_seq: int) -> list[StoredMessage]:
        """Atomically commit pending inbox deliveries and return messages after a cursor.

        Args:
            id: Session identifier whose inbox should be committed.
            after_seq: Exclusive message sequence cursor for returned rows.

        Returns:
            Stored messages with sequence numbers greater than ``after_seq``,
            including rows committed by earlier calls.

        Raises:
            KeyError: If the session does not exist or a stored message omits
                a required field.
            ConfigError: If ``after_seq`` is invalid, a pending row has no
                message payload, or the decoded payload is not an object.
            json.JSONDecodeError: If stored message JSON is malformed.
            TypeError: If a stored message field has an incompatible shape.
            ValueError: If a stored message contains an unknown content-block
                type.
        """

        ...
