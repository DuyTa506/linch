"""Durable-run helpers: event persistence, checkpoint (de)serialization,
resume recovery, and message-identity checks used on resume."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from html import escape
from typing import Any

from .._telemetry import store_call
from .._wire import WIRE_VERSION, require_wire_version
from ..errors import ConfigError
from ..events import (
    BackgroundWorkerEvent,
    Event,
    ToolCallEndEvent,
    ToolCallStartEvent,
    UsageEvent,
    _LoopGuardEvaluationEvent,
)
from ..model_calls import _context_fingerprint, _model_call_fingerprint
from ..run_store import RunCheckpoint, _require_tool_recovery_cursor
from ..session import Session
from ..types import (
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    message_to_dict,
)

_MODEL_CALL_RUNTIME_NAMESPACE = "linch.model_calls.v1"
_MODEL_CALL_RUNTIME_VERSION = WIRE_VERSION
_ACTIVE_PRIMARY_FIELDS = {
    "version",
    "logical_call_id",
    "delivery_attempt_id",
    "turn_index",
    "snapshot_id",
    "request_fingerprint",
    "full_history_base_count",
    "full_history_base_fingerprint",
}


def _model_call_runtime_from_extension(
    extension_state: dict[str, Any],
) -> tuple[dict[str, Any] | None, int | None]:
    """Decode the compact journal-backed accounting envelope."""
    raw = extension_state.get(_MODEL_CALL_RUNTIME_NAMESPACE)
    if raw is None:
        return None, None
    if not isinstance(raw, dict):
        require_wire_version(None)
    require_wire_version(raw.get("version"))
    if set(raw) != {"version", "format", "ledger", "active_primary", "journal_count"}:
        raise ConfigError("invalid model-call checkpoint runtime envelope")
    if raw["format"] != "journal-v1" or raw["ledger"] is not None:
        raise ConfigError("invalid model-call checkpoint runtime envelope")
    if type(raw["journal_count"]) is not int or raw["journal_count"] < 0:
        raise ConfigError("invalid model-call checkpoint runtime envelope")
    active = raw["active_primary"]
    if active is not None:
        _validate_active_primary_binding(active)
    return deepcopy(active), raw["journal_count"]


def _model_call_runtime_to_extension(
    extension_state: dict[str, Any],
    *,
    ledger_snapshot: dict[str, Any] | None,
    active_primary: dict[str, Any] | None,
    journal_count: int = 0,
) -> None:
    """Write the compact, journal-backed accounting envelope into state."""
    if not isinstance(extension_state, dict):
        raise ConfigError("RunCheckpoint.extension_state must be a dict")
    if ledger_snapshot is not None:
        raise ConfigError("journal-v1 runtime envelope cannot include a ledger snapshot")
    if active_primary is not None:
        _validate_active_primary_binding(active_primary)
    if type(journal_count) is not int or journal_count < 0:
        raise ConfigError("invalid model-call journal count")
    extension_state[_MODEL_CALL_RUNTIME_NAMESPACE] = {
        "version": _MODEL_CALL_RUNTIME_VERSION,
        "format": "journal-v1",
        "ledger": None,
        "active_primary": deepcopy(active_primary),
        "journal_count": journal_count,
    }


def _validate_active_primary_binding(raw: Any) -> None:
    if not isinstance(raw, dict):
        require_wire_version(None)
    require_wire_version(raw.get("version"))
    if set(raw) != _ACTIVE_PRIMARY_FIELDS:
        raise ConfigError("invalid active-primary checkpoint binding")
    for name in (
        "logical_call_id",
        "delivery_attempt_id",
        "request_fingerprint",
        "full_history_base_fingerprint",
    ):
        value = raw.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ConfigError("invalid active-primary checkpoint binding")
    snapshot_id = raw.get("snapshot_id")
    if snapshot_id is not None and (not isinstance(snapshot_id, str) or not snapshot_id.strip()):
        raise ConfigError("invalid active-primary checkpoint binding")
    for name in ("turn_index", "full_history_base_count"):
        value = raw.get(name)
        if type(value) is not int or value < 0:
            raise ConfigError("invalid active-primary checkpoint binding")


def _message_digest(message: Message) -> str:
    return _model_call_fingerprint(message_to_dict(message))


def _receipt_message_prefix(
    messages: list[Message],
    *,
    base_count: int,
    base_fingerprint: str,
    expected_digests: list[str],
) -> int:
    """Match only an ordered durable prefix at a receipt's exact history boundary.

    Reliability can share this identity check for prefix0/1/2 without enabling
    its feedback policy in terminal_error_v0 accounting runs.
    """
    if (
        type(base_count) is not int
        or base_count < 0
        or len(messages) < base_count
        or _context_fingerprint(messages[:base_count]) != base_fingerprint
    ):
        raise ConfigError("receipt recovery durable message base does not match")
    suffix = messages[base_count:]
    if len(suffix) > len(expected_digests) or any(
        _message_digest(message) != expected_digests[index] for index, message in enumerate(suffix)
    ):
        raise ConfigError("receipt recovery has an unexpected durable message suffix")
    return len(suffix)


def _primary_control_message(message: Message, feedback: str | None) -> Message:
    """The existing AfterProviderCall continue tail, derived without inference."""
    from ..skills.system_reminder import wrap_in_system_reminder

    tools = [block for block in message.content if isinstance(block, ToolUseBlock)]
    if not tools:
        return Message(
            role="user", content=[TextBlock(text=wrap_in_system_reminder(feedback or ""))]
        )
    return Message(
        role="user",
        content=[
            ToolResultBlock(
                tool_use_id=block.id,
                content=(feedback or "Provider hook requested another turn.")
                if index == 0
                else "Tool not executed; revise and resubmit your final answer.",
                is_error=True,
            )
            for index, block in enumerate(tools)
        ],
    )


class RunEventBuffer:
    """Per-run event buffer.

    Observational events are appended and flushed in batches (fewer store round
    trips); recovery-critical tool start/end events are flushed immediately by
    the runner so they stay durable at the yield boundary. Per-run (attached to
    the session for one run), never process-global.
    """

    __slots__ = ("store", "run_id", "_pending", "_last_seq", "session")

    def __init__(
        self, store: Any, run_id: str, *, initial_seq: int = 0, session: Any = None
    ) -> None:
        self.store = store
        self.session = session
        self.run_id = run_id
        self._pending: list[Event] = []
        self._last_seq = max(0, initial_seq)

    @property
    def last_seq(self) -> int:
        """Highest durable event seq flushed so far (the tool-batch cursor base)."""
        return self._last_seq

    async def append(self, event: Event) -> None:
        if self.store is not None:
            self._pending.append(event)

    async def flush(self) -> int:
        if self.store is None or not self._pending:
            return self._last_seq
        pending = self._pending
        self._pending = []
        appender = getattr(self.store, "append_events", None)
        if appender is not None:
            try:
                seqs = await store_call(
                    self.session, "run.append_events", appender(self.run_id, pending)
                )
            except BaseException:
                self._pending = pending + self._pending
                raise
        else:
            seqs = []
            for index, event in enumerate(pending):
                try:
                    seq = await store_call(
                        self.session,
                        "run.append_event",
                        self.store.append_event(self.run_id, event),
                    )
                except BaseException:
                    self._pending = pending[index:] + self._pending
                    raise
                seqs.append(seq)
                self._last_seq = max(self._last_seq, seq)
        if seqs:
            # max (not seqs[-1]): a test store may return 0 for a dropped event;
            # keep the watermark monotonic.
            self._last_seq = max(self._last_seq, *seqs)
        return self._last_seq


async def _persist_event(session: Session, run_id: str, event: Event) -> None:
    buf = getattr(session, "_event_journal", None)
    if buf is not None and buf.run_id == run_id:
        await buf.append(event)
        return
    store = session.agent.run_store
    if store is not None:
        await store_call(session, "run.append_event", store.append_event(run_id, event))


async def _flush_events(session: Session) -> None:
    """Flush the run's buffered events to the store (no-op without a buffer)."""
    buf = getattr(session, "_event_journal", None)
    if buf is not None:
        await buf.flush()


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
    if not hasattr(session, "notify"):
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
        # Delivery precedes audit so a completion that reached the durable inbox
        # is never represented only by observability metadata.
        await session.notify(
            _crashed_worker_notification(worker_id, display_name),
            delivery_id=f"subagent:{worker_id}",
            source="subagent-recovery",
            metadata={"worker_id": worker_id, "status": "killed"},
        )
        await _persist_event(session, run_id, event)
        raw["status"] = "killed"
        events.append(event)
    return events


_LOOP_GUARD_RUNTIME_NAMESPACE = "linch.loop_guard.v1"
_LOOP_GUARD_RUNTIME_FORMAT = "delta-v1"


def _loop_guard_is_active(guard: Any) -> bool:
    return guard is not None and (
        getattr(guard, "max_identical_tool_calls", 0) > 0
        or getattr(guard, "max_consecutive_failures", 0) > 0
    )


def _loop_guard_runtime_from_extension(extension_state: dict[str, Any]) -> tuple[int, int]:
    """Decode the compact committed-prefix watermark for guard deltas."""
    raw = extension_state.get(_LOOP_GUARD_RUNTIME_NAMESPACE)
    if raw is None:
        return 0, 0
    if not isinstance(raw, dict):
        raise ConfigError("invalid loop-guard checkpoint runtime envelope")
    if set(raw) != {"version", "format", "evaluation_count", "evaluation_seq"}:
        raise ConfigError("invalid loop-guard checkpoint runtime envelope")
    require_wire_version(raw.get("version"))
    if raw["format"] != _LOOP_GUARD_RUNTIME_FORMAT:
        raise ConfigError("invalid loop-guard checkpoint runtime envelope")
    count = raw["evaluation_count"]
    seq = raw["evaluation_seq"]
    if type(count) is not int or count < 0 or type(seq) is not int or seq < 0:
        raise ConfigError("invalid loop-guard checkpoint runtime envelope")
    return count, seq


def _loop_guard_runtime_to_extension(
    extension_state: dict[str, Any], *, evaluation_count: int, evaluation_seq: int
) -> None:
    """Write the scalar committed-prefix watermark, never the guard map."""
    if not isinstance(extension_state, dict):
        raise ConfigError("RunCheckpoint.extension_state must be a dict")
    if type(evaluation_count) is not int or evaluation_count < 0:
        raise ConfigError("invalid loop-guard evaluation count")
    if type(evaluation_seq) is not int or evaluation_seq < 0:
        raise ConfigError("invalid loop-guard evaluation sequence")
    extension_state[_LOOP_GUARD_RUNTIME_NAMESPACE] = {
        "version": WIRE_VERSION,
        "format": _LOOP_GUARD_RUNTIME_FORMAT,
        "evaluation_count": evaluation_count,
        "evaluation_seq": evaluation_seq,
    }


def _loop_guard_batch_id(turn_index: int, tool_blocks: list[ToolUseBlock]) -> str:
    """Return a stable identity for one accepted assistant tool batch."""
    if type(turn_index) is not int or turn_index < 0:
        raise ConfigError("loop-guard batch turn index is invalid")
    return _model_call_fingerprint(
        {
            "turn_index": turn_index,
            "tool_blocks": [
                {"id": block.id, "name": block.name, "input": block.input} for block in tool_blocks
            ],
        }
    )


def _loop_guard_batch_id_for_checkpoint(checkpoint: RunCheckpoint) -> str | None:
    """Return the current in-flight batch identity, if recovery owns one."""
    if checkpoint.phase not in {
        "assistant_appended",
        "permission_pending",
        "tool_batch_pending",
        "tool_executing",
        "tool_results_appended",
    }:
        return None
    if checkpoint.phase == "assistant_appended":
        blocks = []
        if checkpoint.assistant_message:
            blocks = [
                block
                for block in checkpoint.assistant_message.content
                if isinstance(block, ToolUseBlock)
            ]
    else:
        blocks = list(checkpoint.pending_tool_blocks)
    return _loop_guard_batch_id(checkpoint.turn_index, blocks) if blocks else None


def _loop_guard_delta_rows(rows: list[Any]) -> list[tuple[int, _LoopGuardEvaluationEvent]]:
    """Validate and return private guard facts in durable sequence order."""
    out: list[tuple[int, _LoopGuardEvaluationEvent]] = []
    seen: dict[str, _LoopGuardEvaluationEvent] = {}
    previous_seq = 0
    for row in rows:
        seq = getattr(row, "seq", None)
        if type(seq) is not int or seq < 1:
            raise ConfigError("loop-guard journal requires valid event sequence numbers")
        if seq < previous_seq:
            raise ConfigError("loop-guard journal sequence numbers are not ordered")
        previous_seq = seq
        event = getattr(row, "event", None)
        if not isinstance(event, _LoopGuardEvaluationEvent):
            continue
        prior = seen.get(event.batch_id)
        if prior is not None and prior != event:
            raise ConfigError("conflicting loop-guard delta for one batch")
        if out and event.turn_index < out[-1][1].turn_index:
            raise ConfigError("loop-guard delta turn indexes are not ordered")
        seen[event.batch_id] = event
        out.append((seq, event))
    return out


def _loop_guard_evaluations_from_events(
    rows: list[Any],
    *,
    after_seq: int = 0,
) -> dict[str, _LoopGuardEvaluationEvent]:
    if type(after_seq) is not int or after_seq < 0:
        raise ConfigError("invalid loop-guard evaluation sequence")
    return {event.batch_id: event for seq, event in _loop_guard_delta_rows(rows) if seq > after_seq}


def _validate_loop_guard_delta_decision(
    guard: Any,
    state: Any,
    event: _LoopGuardEvaluationEvent,
    changed_counts: dict[str, int],
) -> None:
    """Reject a malformed fact instead of silently changing guard behavior."""
    from ..loop_guard import LoopGuardState

    if not isinstance(state, LoopGuardState):
        raise ConfigError("invalid loop-guard replay state")
    if event.action == "continue":
        if guard.max_identical_tool_calls > 0 and any(
            state.call_counts.get(signature, 0) >= guard.max_identical_tool_calls
            for signature in changed_counts
        ):
            raise ConfigError("loop-guard delta continues after repeated call threshold")
        if (
            guard.max_consecutive_failures > 0
            and state.consecutive_failures >= guard.max_consecutive_failures
        ):
            raise ConfigError("loop-guard delta continues after failure threshold")
        return
    if event.reason not in {"repeated_tool_call", "repeated_failures"} or not event.detail:
        raise ConfigError("loop-guard delta has an invalid decision")
    repeated_call = guard.max_identical_tool_calls > 0 and any(
        state.call_counts.get(signature, 0) >= guard.max_identical_tool_calls
        for signature in changed_counts
    )
    repeated_failure = (
        guard.max_consecutive_failures > 0
        and state.consecutive_failures >= guard.max_consecutive_failures
    )
    if event.reason == "repeated_tool_call" and not repeated_call:
        raise ConfigError("loop-guard delta lacks a repeated-call threshold crossing")
    if event.reason == "repeated_failures" and (not repeated_failure or repeated_call):
        raise ConfigError("loop-guard delta lacks the expected failure threshold crossing")


def _loop_guard_state_from_events(
    guard: Any,
    rows: list[Any],
    *,
    evaluation_count: int,
    evaluation_seq: int,
    current_batch_id: str | None,
    current_turn_index: int | None = None,
) -> Any:
    """Rebuild guard state from the checkpoint-committed sparse delta prefix.

    Tool scheduler events are intentionally not an input here: they can be
    missing across a valid crash cut, and their recovery cursor is unrelated to
    the point at which the guard was evaluated.  Facts after the committed
    watermark are allowed only for the one in-flight batch and are replayed by
    the normal tool phase.
    """
    from ..loop_guard import LoopGuardState

    if type(evaluation_count) is not int or evaluation_count < 0:
        raise ConfigError("invalid loop-guard evaluation count")
    if type(evaluation_seq) is not int or evaluation_seq < 0:
        raise ConfigError("invalid loop-guard evaluation sequence")
    delta_rows = _loop_guard_delta_rows(rows)
    committed: list[tuple[int, _LoopGuardEvaluationEvent]] = []
    ahead_ids: set[str] = set()
    unique_committed: set[str] = set()
    for seq, event in delta_rows:
        if seq <= evaluation_seq:
            if event.batch_id not in unique_committed:
                unique_committed.add(event.batch_id)
                committed.append((seq, event))
        else:
            ahead_ids.add(event.batch_id)
    if len(unique_committed) != evaluation_count:
        raise ConfigError("loop-guard journal is missing committed evaluation facts")
    if current_batch_id is None:
        if ahead_ids:
            raise ConfigError("loop-guard journal is ahead of a non-tool checkpoint")
    else:
        if any(batch_id != current_batch_id for batch_id in ahead_ids):
            raise ConfigError("loop-guard journal is ahead of a different tool batch")
        if current_turn_index is not None and any(
            event.turn_index != current_turn_index
            for seq, event in delta_rows
            if seq > evaluation_seq and event.batch_id == current_batch_id
        ):
            raise ConfigError("loop-guard journal fact does not match the checkpoint turn")
    if current_batch_id in unique_committed:
        raise ConfigError("in-flight tool batch is already committed in loop-guard checkpoint")

    state = LoopGuardState()
    for _, event in committed:
        changed_counts = dict(event.count_deltas)
        if state.consecutive_failures != event.failures_before:
            raise ConfigError("loop-guard delta failure streak does not match its prefix")
        for signature, delta in changed_counts.items():
            if state.call_counts.get(signature, 0) != event.count_before[signature]:
                raise ConfigError("loop-guard delta count does not match its prefix")
            state.call_counts[signature] = state.call_counts.get(signature, 0) + delta
        state.consecutive_failures = event.consecutive_failures
        _validate_loop_guard_delta_decision(guard, state, event, changed_counts)
    return state


def _consumed_auxiliary_receipt_ids_from_events(rows: list[Any]) -> tuple[str, ...]:
    """Extract and validate the durable usage-projection watermark."""
    ids: list[str] = []
    for row in rows:
        event = getattr(row, "event", None)
        if not isinstance(event, UsageEvent) or event.source != "compaction":
            continue
        if not isinstance(event.call_id, str) or not event.call_id.strip():
            raise ConfigError("compaction usage event lacks a receipt call ID")
        ids.append(event.call_id)
    if len(set(ids)) != len(ids):
        raise ConfigError("duplicate compaction usage event receipt IDs")
    return tuple(ids)


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


def _alignment_queue_to_dicts(session: Session) -> list[dict[str, Any]]:
    return [
        {"prompt": entry.prompt, "images": entry.images}
        for entry in getattr(session, "alignment_queue", []) or []
    ]


def _alignment_entries_from_checkpoint(raw: list[dict[str, Any]]) -> list[Any]:
    from ..session import AlignmentEntry

    entries: list[Any] = []
    for item in raw:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        # Nobody awaits a restored entry's future; pre-cancel it so every
        # set_result/set_exception guard (drain, abort, run-end rejection) is a
        # silent no-op and GC never logs "exception was never retrieved".
        future.cancel()
        entries.append(
            AlignmentEntry(prompt=item["prompt"], images=item.get("images"), future=future)
        )
    return entries


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
    last = session.last_provider_message()
    return last is not None and _message_matches(last, message)


def _last_message_has_tool_results(session: Session, tool_blocks: list[ToolUseBlock]) -> bool:
    message = session.last_provider_message()
    if message is None:
        return False
    if message.role != "user":
        return False
    results = [block for block in message.content if isinstance(block, ToolResultBlock)]
    return {block.tool_use_id for block in results} == {block.id for block in tool_blocks}


async def _recover_completed_tool_results(
    session: Session,
    run_id: str,
    completed: dict[str, ToolResultBlock],
    tool_blocks: list[ToolUseBlock],
    *,
    after_seq: int | None,
) -> dict[str, ToolResultBlock]:
    """Reconstruct a resumed turn's completed tool results from durable state.

    The event log is the recovery source for tool execution: a ``ToolCallEndEvent``
    contributes its recorded result, and a ``ToolCallStartEvent`` with no matching
    end becomes the interrupted-before-result placeholder so a started-but-
    unfinished tool is not blindly re-run on resume. Results already carried by the
    checkpoint (older stores, or the ``tool_results_appended`` checkpoint) are kept
    and never downgraded to a placeholder.

    The scan is bounded to events after ``after_seq`` (the checkpoint's tool-batch
    cursor) and restricted to tool-use ids/names in ``tool_blocks`` with a valid
    start→end ordering, so a tool-use id that recurs across turns is unambiguous.

    Args:
        completed: Results already present on the resumed checkpoint.
        tool_blocks: The current turn's tool-use blocks (the membership set).
        after_seq: Required non-negative event-seq cursor from the checkpoint.

    Returns:
        The tool_use_id → result-block map for the current tool batch, merging the
        checkpoint results with everything recoverable from the event log.
    """
    store = session.agent.run_store
    if store is None:
        return dict(completed)
    after_seq = _require_tool_recovery_cursor(after_seq)
    membership = {block.id: block.name for block in tool_blocks}
    recovered = dict(completed)
    started: dict[str, ToolCallStartEvent] = {}
    ended: set[str] = set()
    for stored in await store_call(
        session, "run.load_events", store.load_events(run_id, after_seq=after_seq)
    ):
        event = stored.event
        if (
            isinstance(event, ToolCallStartEvent)
            and membership.get(event.tool_use_id) == event.tool_name
        ):
            started[event.tool_use_id] = event
        elif (
            isinstance(event, ToolCallEndEvent)
            and membership.get(event.tool_use_id) == event.tool_name
            and event.tool_use_id in started  # valid start→end ordering
        ):
            ended.add(event.tool_use_id)
            recovered[event.tool_use_id] = _tool_result_block_from_end(event)
    for tool_use_id, start_event in started.items():
        if tool_use_id not in ended:
            recovered.setdefault(tool_use_id, _interrupted_tool_result_block(start_event))
    return recovered
