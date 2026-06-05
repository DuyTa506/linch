from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from .types import AgentDefinition

WorkerStatus = Literal["running", "completed", "failed", "killed"]


@dataclass
class WorkerHandle:
    """In-process handle to a retained subagent worker."""

    worker_id: str
    child_session_id: str
    display_name: str
    definition: AgentDefinition
    status: WorkerStatus = "running"
    task: Any = field(default=None, repr=False)  # asyncio.Task | None
    last_result_text: str = ""
