from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..subagents.workers import WorkerHandle


def resolve_worker(session: Any, target: str) -> WorkerHandle | None:
    """Resolve a worker by its exact id or a unique display name.

    Args:
        session: Parent session containing the worker registry.
        target: Worker id or display name supplied by the caller.

    Returns:
        The matching worker handle, or ``None`` when no worker matches.

    Raises:
        ValueError: If more than one worker has the requested display name.
    """
    workers: dict[str, Any] = getattr(session, "workers", {})
    handle = workers.get(target)
    if handle is not None:
        return handle
    matches = [h for h in workers.values() if h.display_name == target]
    if len(matches) > 1:
        ids = ", ".join(str(handle.worker_id) for handle in matches)
        raise ValueError(f"Worker display name {target!r} is ambiguous; use a worker id: {ids}.")
    if matches:
        return matches[0]
    return None
