"""Resume reconciliation for the agent run loop.

This phase proves the durable provider/tool boundary before the turn
choreography starts again.  It yields recovery events at their original
boundaries; it does not buffer or reorder them.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any

from ..compaction import _reconcile_paid_compactions
from ..errors import ConfigError
from ..events import ErrorEvent, Event
from ..model_calls import _ModelCallLedger, _primary_request_fingerprint
from ..types import Message, ProviderRequest, TextBlock, ToolResultBlock, ToolUseBlock
from .checkpoint import (
    _message_digest,
    _primary_control_message,
    _queue_crashed_worker_notifications,
    _receipt_message_prefix,
)
from .feedback import append_feedback
from .turn_stopping import INVALID_RESPONSE_DIAGNOSTIC, TurnStopping


@dataclass(slots=True)
class ResumeState:
    """Mutable recovery outcome consumed by the turn choreography."""

    start_turn: int
    recovered_primary_projection: dict[str, Any] | None = None
    recovered_primary_prefix: int = 0
    recovered_compaction: bool = False
    terminal_event: Event | None = None
    terminal_kind: str | None = None
    terminal_feedback: str | None = None
    terminal: bool = False
    replayed_feedback: tuple[Message, ...] = ()
    invalid_feedback: tuple[Message, ...] = ()


@dataclass(slots=True)
class ResumeContext:
    """Run-local callbacks and state needed by :func:`recover_resume`."""

    session: Any
    agent: Any
    config: Any
    run_id: str
    run_record: Any
    stored_events: list[Any]
    checkpoint: Any
    resume_checkpoint: Any
    ledger: _ModelCallLedger
    signal: Any
    exact_model_input: bool
    active_primary: dict[str, Any] | None
    load_pending_provider_request: Callable[[Any], Awaitable[ProviderRequest]]
    maybe_save_provider_snapshot: Callable[..., Awaitable[None]]
    persist_event: Callable[[Event], Awaitable[None]]
    emit_error_terminal: Callable[..., AsyncGenerator[Event, None]]
    drain_auxiliary_usage: Callable[[], AsyncGenerator[Event, None]]
    save_checkpoint: Callable[..., Awaitable[None]]
    turn_stopping: Any
    state: ResumeState


def _hook_retry_was_recorded(ctx: ResumeContext, assistant: Message) -> bool:
    """Identify a persisted BeforeFinalAnswer retry for the resumed assistant."""
    assistant_digest = _message_digest(assistant)
    assistant_index = -1
    for index, row in enumerate(ctx.stored_events):
        event = getattr(row, "event", row)
        if (
            getattr(event, "type", None) == "assistant"
            and getattr(event, "message", None) is not None
            and _message_digest(event.message) == assistant_digest
        ):
            assistant_index = index
    if assistant_index < 0:
        return False
    return any(
        getattr(getattr(row, "event", row), "type", None) == "hook"
        and getattr(getattr(row, "event", row), "event", None) == "BeforeFinalAnswer"
        and getattr(getattr(row, "event", row), "action", None) in {"retry", "force_continue"}
        for row in ctx.stored_events[assistant_index + 1 :]
    )


def _is_final_tool_ack(message: Message, assistant: Message) -> bool:
    """Return whether *message* acknowledges a terminal tool in *assistant*."""
    tool_ids = {block.id for block in assistant.content if isinstance(block, ToolUseBlock)}
    if not tool_ids:
        return False
    results = [block for block in message.content if isinstance(block, ToolResultBlock)]
    return bool(results) and all(
        block.tool_use_id in tool_ids
        and block.content == "Final answer captured by the host."
        and not block.is_error
        for block in results
    )


def _turn_stopping_feedback_tail(ctx: ResumeContext) -> tuple[Message, ...]:
    """Recover feedback after a checkpointed assistant turn-stopping decision."""
    if ctx.checkpoint.phase != "assistant_appended":
        return ()
    resume_state = ctx.checkpoint.extension_state.get("linch.resume", {})
    if not (
        isinstance(resume_state, dict) and resume_state.get("feedback_origin") == "turn_stopping"
    ):
        return ()
    assistant = ctx.checkpoint.assistant_message
    if assistant is None:
        return ()
    history = list(ctx.session.full_history)
    base_count = resume_state.get("feedback_base_count")
    if type(base_count) is not int or base_count <= 0 or base_count > len(history):
        raise ConfigError("invalid turn-stopping feedback boundary")
    if _message_digest(history[base_count - 1]) != _message_digest(assistant):
        raise ConfigError("turn-stopping feedback boundary differs from its assistant")
    return tuple(history[base_count:])


async def recover_resume(ctx: ResumeContext) -> AsyncGenerator[Event, None]:
    """Reconcile a resumed run, yielding recovery events in durable order."""
    if ctx.resume_checkpoint is None:
        return

    ctx.state.recovered_compaction = await _reconcile_paid_compactions(
        ctx.session,
        ctx.agent,
        ctx.ledger,
        ctx.checkpoint.turn_index,
    )
    if ctx.state.recovered_compaction:
        await ctx.maybe_save_provider_snapshot(ctx.session)

    if ctx.checkpoint.phase in {"provider_pending", "assistant_appended"}:
        await _reconcile_active_primary(ctx)
        if ctx.active_primary is None:
            ctx.state.replayed_feedback = _turn_stopping_feedback_tail(ctx)
        if ctx.state.terminal_kind in {"invalid_text", "rejected_native"}:
            decision = await ctx.turn_stopping.dispatch(
                TurnStopping(ctx.session, ctx.run_id, ctx.checkpoint.turn_index, "invalid_response")
            )
            async with aclosing(
                append_feedback(
                    ctx.session,
                    ctx.run_id,
                    ctx.state.invalid_feedback or decision.feedback,
                    boundary=ctx.active_primary,
                    decision=decision,
                )
            ) as feedback_events:
                async for event in feedback_events:
                    yield event
            if decision.action == "continue":
                await ctx.save_checkpoint("turn_complete", turn_index=ctx.checkpoint.turn_index)
                ctx.state.start_turn = ctx.checkpoint.turn_index + 1
                return
            assert ctx.state.terminal_event is not None
            await ctx.persist_event(ctx.state.terminal_event)
            yield ctx.state.terminal_event
            async with aclosing(ctx.emit_error_terminal()) as error_events:
                async for event in error_events:
                    yield event
            ctx.state.terminal = True
            return
        if ctx.state.terminal_kind == "stopped":
            async with aclosing(
                ctx.emit_error_terminal(final_text_value=ctx.state.terminal_feedback)
            ) as error_events:
                async for event in error_events:
                    yield event
            ctx.state.terminal = True
            return
        if ctx.state.terminal:
            return

    async with aclosing(ctx.drain_auxiliary_usage()) as usage_events:
        async for usage_event in usage_events:
            yield usage_event

    # Keep this yield inside the caller's try/finally.  A consumer can close
    # exactly at a worker notification and still get lifecycle cleanup.
    for worker_event in await _queue_crashed_worker_notifications(
        ctx.session,
        ctx.checkpoint,
        ctx.run_id,
    ):
        yield worker_event
    if ctx.checkpoint.background_workers:
        await ctx.save_checkpoint(ctx.checkpoint.phase, turn_index=ctx.checkpoint.turn_index)


async def _reconcile_active_primary(ctx: ResumeContext) -> None:
    """Validate and, where possible, finish a frozen primary delivery."""
    active_primary = ctx.active_primary
    if active_primary is None:
        if ctx.checkpoint.phase == "provider_pending":
            raise ConfigError("provider_pending checkpoint lacks its active primary binding")
        return
    if active_primary["turn_index"] != ctx.checkpoint.turn_index:
        raise ConfigError("active primary turn does not match its checkpoint")
    if ctx.checkpoint.phase == "provider_pending":
        if active_primary["snapshot_id"] != ctx.checkpoint.model_input_snapshot_id:
            raise ConfigError("active primary snapshot does not match its checkpoint")
        if ctx.exact_model_input:
            frozen_request = await ctx.load_pending_provider_request(ctx.signal)
            if (
                _primary_request_fingerprint(frozen_request)
                != active_primary["request_fingerprint"]
            ):
                raise ConfigError("active primary request does not match its frozen snapshot")

    receipt = ctx.ledger.receipt_for(active_primary["delivery_attempt_id"])
    intent = ctx.ledger.intent_for(active_primary["delivery_attempt_id"])
    for identity in (receipt, intent):
        if identity is not None and any(
            getattr(identity, name) != active_primary[name]
            for name in (
                "logical_call_id",
                "delivery_attempt_id",
                "turn_index",
                "request_fingerprint",
            )
        ):
            raise ConfigError("active primary identity differs from its receipt or intent")

    matches = [
        value
        for value in ctx.ledger.projections("primary_append")
        if value["delivery_attempt_id"] == active_primary["delivery_attempt_id"]
    ]
    projection = matches[0] if matches else None
    if projection is not None:
        if any(
            projection[name] != value for name, value in active_primary.items() if name != "version"
        ):
            raise ConfigError("primary acceptance projection differs from its active binding")
        expected = [] if projection["after_action"] == "stop" else [projection["assistant_sha256"]]
        if projection["after_action"] == "continue":
            expected.append(projection["control_message_sha256"])
    else:
        expected = []
    if receipt is not None and receipt.disposition == "rejected_native":
        ctx.state.terminal_event = ErrorEvent(
            error={
                "name": "ProviderError",
                "message": "provider response contained malformed native tool arguments",
                "retryable": False,
            }
        )
        ctx.state.terminal_kind = "rejected_native"
        return
    if projection is not None and projection["after_action"] == "continue":
        diagnostic = Message(
            role="assistant", content=[TextBlock(text=INVALID_RESPONSE_DIAGNOSTIC)]
        )
        control = _primary_control_message(diagnostic, projection["after_feedback"], wrap=False)
        if expected == [_message_digest(diagnostic), _message_digest(control)]:
            ctx.state.terminal_kind = "invalid_text"
            ctx.state.invalid_feedback = (diagnostic, control)
            return
    history = list(ctx.session.full_history)
    base = active_primary["full_history_base_count"]
    if ctx.checkpoint.phase == "assistant_appended" and len(history) > base + len(expected):
        replayed_tail = tuple(history[base + len(expected) :])
        assistant = ctx.checkpoint.assistant_message
        resume_state = ctx.checkpoint.extension_state.get("linch.resume", {})
        turn_stopping_pending = (
            isinstance(resume_state, dict)
            and resume_state.get("feedback_origin") == "turn_stopping"
        )
        if assistant is not None:
            replayed_tail = tuple(
                message for message in replayed_tail if not _is_final_tool_ack(message, assistant)
            )
        if not turn_stopping_pending and (
            assistant is None
            or ctx.checkpoint.pending_truncation_feedback is not None
            or _hook_retry_was_recorded(ctx, assistant)
        ):
            replayed_tail = ()
        ctx.state.replayed_feedback = replayed_tail
        history = history[: base + len(expected)]
    ctx.state.recovered_primary_prefix = _receipt_message_prefix(
        history,
        base_count=base,
        base_fingerprint=active_primary["full_history_base_fingerprint"],
        expected_digests=expected,
    )
    if projection is not None and projection["after_action"] == "stop":
        ctx.state.terminal_kind = "stopped"
        ctx.state.terminal_feedback = projection["after_feedback"]
        return
    if ctx.state.recovered_primary_prefix:
        assert projection is not None
        ctx.state.recovered_primary_projection = projection
        ctx.checkpoint.assistant_message = ctx.session.full_history[
            active_primary["full_history_base_count"]
        ]
        ctx.checkpoint.assistant_stop_reason = projection["assistant_stop_reason"]
        if ctx.checkpoint.phase == "provider_pending":
            await ctx.save_checkpoint(
                "assistant_appended",
                turn_index=ctx.checkpoint.turn_index,
                assistant_message=ctx.checkpoint.assistant_message,
                assistant_stop_reason=ctx.checkpoint.assistant_stop_reason,
            )
