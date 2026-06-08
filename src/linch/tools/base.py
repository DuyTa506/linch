from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

ToolScope = Literal["read", "write", "exec"]
ResourceMode = Literal["read", "write"]


@dataclass(frozen=True, slots=True)
class ResourceAccess:
    resource: str
    mode: ResourceMode = "read"


@dataclass(slots=True)
class Citation:
    id: str
    source: str
    label: str | None = None
    chunk: str | None = None
    score: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ToolContext:
    cwd: str
    session_id: str
    run_id: str
    session_store: Any
    signal: Any = None
    file_read_tracker: Any = None
    emit: Callable[..., None] | None = None
    deps: Any = None
    """Application-state dependency object injected via ``Agent(deps=...)``
    or ``RunOptions(deps=...)``.  Use this to share a vector-store client,
    database connection, or any other per-agent / per-run resource across
    all tool calls without requiring ``__init__``-closure injection."""

    filesystem: Any = None
    """Per-session virtual :class:`~linch.filesystem.backend.FileBackend`,
    when the filesystem subsystem is enabled (``Agent(filesystem=...)`` or
    ``Agent(result_offload=...)``).  The ls / read_file / write_file / edit_file
    tools read and write through this, and the scheduler offloads oversized
    results here.  ``None`` when the subsystem is off."""

    @property
    def sessionId(self) -> str:
        return self.session_id

    @property
    def runId(self) -> str:
        return self.run_id

    @property
    def sessionStore(self) -> Any:
        return self.session_store

    @property
    def fileReadTracker(self) -> Any:
        return self.file_read_tracker


@dataclass(slots=True)
class ToolResult:
    content: str
    summary: str = ""
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    citations: list[Citation] = field(default_factory=list)
    attachments: list[Any] = field(default_factory=list)
    duration_ms: int = 0
    truncated: bool = False
    recovery_hint: str = ""


class Tool(Protocol):
    name: str
    description: str
    input_schema: dict[str, Any]
    scope: ToolScope
    parallel: bool

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]: ...

    async def execute(self, input: dict[str, Any], ctx: ToolContext) -> ToolResult: ...

    def summarize(self, input: dict[str, Any]) -> str: ...


def require_str(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or value == "":
        raise ValueError(f"{key} must be a non-empty string")
    return value
