from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..subagents.workers import WorkerHandle


def resolve_worker(session: Any, target: str) -> WorkerHandle | None:
    """Return the WorkerHandle matching *target* by worker_id or display_name."""
    workers: dict[str, Any] = getattr(session, "workers", {})
    handle = workers.get(target)
    if handle is not None:
        return handle
    for h in workers.values():
        if h.display_name == target:
            return h
    return None
