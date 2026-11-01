from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

TaskStatus = Literal["pending", "in_progress", "completed"]


@dataclass(slots=True)
class Task:
    """Represent one session task and its dependency relationships.

    Args:
        id: Stable task identifier within the session.
        session_id: Identifier of the owning session.
        subject: Short task title.
        description: Detailed task instructions or context.
        active_form: Present-tense label shown while the task is active.
        status: Lifecycle state of the task.
        owner: Worker or caller that currently claims the task.
        blocks: Task IDs that this task blocks.
        blocked_by: Task IDs that must complete before this task is ready.
        metadata: Application-specific task metadata.
        created_at: Creation timestamp in ISO format.
        updated_at: Most recent modification timestamp in ISO format.

    Attributes:
        id: Stable task identifier within the session.
        session_id: Identifier of the owning session.
        subject: Short task title.
        description: Detailed task instructions or context.
        active_form: Present-tense label shown while the task is active.
        status: Lifecycle state of the task.
        owner: Worker or caller that currently claims the task.
        blocks: Task IDs that this task blocks.
        blocked_by: Task IDs that must complete before this task is ready.
        metadata: Application-specific task metadata.
        created_at: Creation timestamp in ISO format.
        updated_at: Most recent modification timestamp in ISO format.
    """

    id: str
    session_id: str
    subject: str
    description: str
    active_form: str | None = None
    status: TaskStatus = "pending"
    owner: str | None = None
    blocks: list[str] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)
    metadata: dict[str, object] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""


@dataclass(slots=True)
class CreateTaskInput:
    """Describe the fields used to create a pending task.

    Args:
        subject: Short task title.
        description: Detailed task instructions or context.
        active_form: Present-tense label shown while the task is active.
        metadata: Optional application-specific task metadata.

    Attributes:
        subject: Short task title.
        description: Detailed task instructions or context.
        active_form: Present-tense label shown while the task is active.
        metadata: Optional application-specific task metadata.
    """

    subject: str
    description: str
    active_form: str | None = None
    metadata: dict[str, object] | None = None


@dataclass(slots=True)
class TaskPatch:
    """Describe partial updates to a task and its dependency edges.

    Args:
        subject: Replacement task title, or ``None`` to leave it unchanged.
        description: Replacement task description, or ``None`` to leave it unchanged.
        active_form: Replacement active label, or ``None`` to leave it unchanged.
        status: Replacement lifecycle state, ``"deleted"``, or ``None`` to leave it unchanged.
        owner: Replacement owner, or ``None`` to leave ownership unchanged.
        add_blocks: Task IDs to add to this task's blocked targets.
        add_blocked_by: Task IDs to add as prerequisites for this task.
        remove_blocks: Task IDs to remove from this task's blocked targets.
        remove_blocked_by: Task IDs to remove as prerequisites for this task.
        metadata: Metadata updates; a value of ``None`` removes that key.

    Attributes:
        subject: Replacement task title, or ``None`` to leave it unchanged.
        description: Replacement task description, or ``None`` to leave it unchanged.
        active_form: Replacement active label, or ``None`` to leave it unchanged.
        status: Replacement lifecycle state, ``"deleted"``, or ``None`` to leave it unchanged.
        owner: Replacement owner, or ``None`` to leave ownership unchanged.
        add_blocks: Task IDs to add to this task's blocked targets.
        add_blocked_by: Task IDs to add as prerequisites for this task.
        remove_blocks: Task IDs to remove from this task's blocked targets.
        remove_blocked_by: Task IDs to remove as prerequisites for this task.
        metadata: Metadata updates; a value of ``None`` removes that key.
    """

    subject: str | None = None
    description: str | None = None
    active_form: str | None = None
    status: TaskStatus | Literal["deleted"] | None = None
    owner: str | None = None
    add_blocks: list[str] | None = None
    add_blocked_by: list[str] | None = None
    remove_blocks: list[str] | None = None
    remove_blocked_by: list[str] | None = None
    metadata: dict[str, object] | None = None
