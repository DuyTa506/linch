"""Resume reconciliation for the agent run loop.

This phase proves the durable provider/tool boundary before the turn
choreography starts again.  It yields recovery events at their original
boundaries; it does not buffer or reorder them.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..compaction import _reconcile_paid_compactions
from ..errors import ConfigError
from ..events import ErrorEvent, Event
from ..model_calls import _ModelCallLedger, _primary_request_fingerprint
from ..types import Message, ProviderRequest, TextBlock
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
    run_id: str
    run_record: Any
    checkpoint: Any
    resume_checkpoint: Any
    ledger: _ModelCallLedger
    signal: Any
    exact_model_input: bool
    active_primary: dict[str, Any] | None
    load_pending_provider_request: Callable[[Any], Awaitable[ProviderRequest]]
    maybe_save_provider_snapshot: Callable[..., Awaitable[None]]
    persist_event: Callable[[Event], Awaitable[None]]
    emit_error_terminal: Callable[..., AsyncIterator[Event]]
    drain_auxiliary_usage: Callable[[], AsyncIterator[Event]]
    save_checkpoint: Callable[..., Awaitable[None]]
    turn_stopping: Any
    state: ResumeState


async def recover_resume(ctx: ResumeContext) -> AsyncIterator[Event]:
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
        if ctx.state.terminal_kind in {"invalid_text", "rejected_native"}:
            decision = await ctx.turn_stopping.dispatch(
                TurnStopping(ctx.session, ctx.run_id, ctx.checkpoint.turn_index, "invalid_response")
            )
            async for event in append_feedback(
                ctx.session,
                ctx.run_id,
                ctx.state.invalid_feedback or decision.feedback,
                boundary=ctx.active_primary,
                decision=decision,
            ):
                yield event
            if decision.action == "continue":
                await ctx.save_checkpoint("turn_complete", turn_index=ctx.checkpoint.turn_index)
                ctx.state.start_turn = ctx.checkpoint.turn_index + 1
                return
            assert ctx.state.terminal_event is not None
            await ctx.persist_event(ctx.state.terminal_event)
            yield ctx.state.terminal_event
            async for event in ctx.emit_error_terminal():
                yield event
            ctx.state.terminal = True
            return
        if ctx.state.terminal_kind == "stopped":
            async for event in ctx.emit_error_terminal(
                final_text_value=ctx.state.terminal_feedback
            ):
                yield event
            ctx.state.terminal = True
            return
        if ctx.state.terminal:
            return

    async for usage_event in ctx.drain_auxiliary_usage():
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
        ctx.state.replayed_feedback = tuple(history[base + len(expected) :])
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
