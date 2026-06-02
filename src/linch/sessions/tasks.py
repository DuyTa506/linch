from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

TaskStatus = Literal["pending", "in_progress", "completed"]


@dataclass(slots=True)
class Task:
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
    subject: str
    description: str
    active_form: str | None = None
    metadata: dict[str, object] | None = None


@dataclass(slots=True)
class TaskPatch:
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
