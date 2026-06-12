"""Durable-run helpers: event persistence, checkpoint (de)serialization,
resume recovery, and message-identity checks used on resume."""

from __future__ import annotations

from html import escape
from typing import Any, cast

from ..events import (
    BackgroundWorkerEvent,
    Event,
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from ..run_store import RunCheckpoint
from ..session import Session
from ..types import (
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    message_to_dict,
)


async def _persist_event(session: Session, run_id: str, event: Event) -> None:
    if session.agent.run_store is not None:
        await session.agent.run_store.append_event(run_id, event)


def _background_workers_to_dict(
    session: Session,
    existing: dict[str, dict[str, object]] | None = None,
) -> dict[str, dict[str, object]]:
    out: dict[str, dict[str, object]] = {
        str(worker_id): dict(metadata) for worker_id, metadata in (existing or {}).items()
    }
    workers = getattr(session, "workers", None)
    if not workers:
        return out
    for worker_id, handle in workers.items():
        out[str(worker_id)] = {
            "worker_id": str(getattr(handle, "worker_id", worker_id)),
            "display_name": str(getattr(handle, "display_name", worker_id)),
            "status": str(getattr(handle, "status", "running")),
            "child_session_id": str(getattr(handle, "child_session_id", "")),
            "last_result_text": str(getattr(handle, "last_result_text", "")),
        }
    return out


def _crashed_worker_notification(worker_id: str, display_name: str) -> Message:
    text = (
        "<task-notification>"
        f"<task-id>{escape(worker_id)}</task-id>"
        "<status>killed</status>"
        f"<summary>Worker '{escape(display_name)}' was interrupted before it finished.</summary>"
        "<error>Worker process was not live when the run resumed.</error>"
        "</task-notification>"
    )
    return Message(role="user", content=[TextBlock(text=text)])


async def _queue_crashed_worker_notifications(
    session: Session,
    checkpoint: RunCheckpoint,
    run_id: str,
) -> list[BackgroundWorkerEvent]:
    if not checkpoint.background_workers:
        return []
    live_workers = getattr(session, "workers", {})
    notifications = getattr(session, "pending_notifications", None)
    if notifications is None:
        return []

    events: list[BackgroundWorkerEvent] = []
    for worker_id, raw in checkpoint.background_workers.items():
        if raw.get("status") != "running" or worker_id in live_workers:
            continue
        display_name = str(raw.get("display_name") or worker_id)
        event = BackgroundWorkerEvent(
            worker_id=worker_id,
            status="killed",
            display_name=display_name,
        )
        # Persist first: if this raises, we have not yet queued a stale
        # notification nor mutated the in-memory checkpoint status.
        await _persist_event(session, run_id, event)
        notifications.append(_crashed_worker_notification(worker_id, display_name))
        raw["status"] = "killed"
        events.append(event)
    return events


def _loop_guard_state_to_dict(state: Any) -> dict[str, object] | None:
    if state is None:
        return None
    return {
        "call_counts": dict(getattr(state, "call_counts", {})),
        "consecutive_failures": int(getattr(state, "consecutive_failures", 0) or 0),
    }


def _loop_guard_state_from_dict(raw: dict[str, object] | None) -> Any:
    if raw is None:
        return None
    from ..loop_guard import LoopGuardState

    call_counts_raw = raw.get("call_counts", {})
    return LoopGuardState(
        call_counts=(
            {str(k): int(v) for k, v in call_counts_raw.items()}
            if isinstance(call_counts_raw, dict)
            else {}
        ),
        consecutive_failures=int(cast(Any, raw.get("consecutive_failures", 0) or 0)),
    )


def _skill_overlay_to_dict(overlay: Any) -> dict[str, object] | None:
    if overlay is None:
        return None
    out: dict[str, object] = {}
    allowed = getattr(overlay, "allowed_tools", None)
    model = getattr(overlay, "model_override", None)
    if allowed is not None:
        out["allowed_tools"] = list(allowed)
    if model is not None:
        out["model_override"] = str(model)
    return out


def _skill_overlay_from_dict(raw: dict[str, object] | None) -> Any:
    if raw is None:
        return None
    from ..types import SkillOverlay

    allowed_raw = raw.get("allowed_tools")
    return SkillOverlay(
        allowed_tools=[str(t) for t in allowed_raw] if isinstance(allowed_raw, list) else None,
        model_override=(
            str(raw.get("model_override")) if isinstance(raw.get("model_override"), str) else None
        ),
    )


def _tool_result_block_from_end(event: ToolCallEndEvent) -> ToolResultBlock:
    return ToolResultBlock(
        tool_use_id=event.tool_use_id,
        content=event.result,
        is_error=event.is_error,
    )


def _interrupted_tool_result_block(event: ToolCallStartEvent) -> ToolResultBlock:
    return ToolResultBlock(
        tool_use_id=event.tool_use_id,
        content=(
            "Tool execution was interrupted before a result was recorded; "
            "not re-running automatically on resume."
        ),
        is_error=True,
    )


def _message_matches(left: Message, right: Message) -> bool:
    return message_to_dict(left) == message_to_dict(right)


def _last_message_matches(session: Session, message: Message) -> bool:
    return bool(session.provider_view) and _message_matches(session.provider_view[-1], message)


def _last_message_has_tool_results(session: Session, tool_blocks: list[ToolUseBlock]) -> bool:
    if not session.provider_view:
        return False
    message = session.provider_view[-1]
    if message.role != "user":
        return False
    results = [block for block in message.content if isinstance(block, ToolResultBlock)]
    return {block.tool_use_id for block in results} == {block.id for block in tool_blocks}


async def _recover_completed_tool_results(
    session: Session,
    run_id: str,
    completed: dict[str, ToolResultBlock],
) -> dict[str, ToolResultBlock]:
    store = session.agent.run_store
    if store is None:
        return dict(completed)
    recovered = dict(completed)
    for stored in await store.load_events(run_id):
        if isinstance(stored.event, ToolCallEndEvent):
            recovered[stored.event.tool_use_id] = _tool_result_block_from_end(stored.event)
    return recovered
