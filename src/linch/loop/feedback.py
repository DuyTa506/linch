"""Canonical continuation messages and rejected-provider recovery projection."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from ..events import AssistantEvent, Event, UserEvent
from ..model_calls import ModelCallReceipt, _projection_record
from ..session import Session
from ..types import AssistantAssembly, Message, StopReason, TextBlock
from .checkpoint import (
    _flush_events,
    _message_digest,
    _persist_event,
    _receipt_message_prefix,
    _replayed_feedback_present,
)
from .turn_stopping import INVALID_RESPONSE_DIAGNOSTIC, TurnStopping, TurnStoppingDecision


async def append_feedback(
    session: Session,
    run_id: str,
    messages: tuple[Message, ...],
    *,
    stop_reason: StopReason = "end_turn",
    paired: bool = False,
    boundary: dict[str, Any] | None = None,
    decision: TurnStoppingDecision | None = None,
) -> AsyncIterator[Event]:
    await _flush_events(session)
    prefix = 0
    if boundary is not None:
        prefix = _receipt_message_prefix(
            list(session.full_history),
            base_count=boundary["full_history_base_count"],
            base_fingerprint=boundary["full_history_base_fingerprint"],
            expected_digests=[_message_digest(message) for message in messages],
        )
    elif _replayed_feedback_present(session, messages):
        prefix = len(messages)
    if messages[prefix:]:
        await session.append(list(messages[prefix:]))
    for index, message in enumerate(messages):
        event: Event = UserEvent(
            message=message, subtype="prompt" if paired and not index else "notification"
        )
        if message.role == "assistant":
            event = AssistantEvent(message=message, stop_reason=stop_reason)
        await _persist_event(session, run_id, event)
        yield event
    if decision is not None and decision.event is not None:
        await _persist_event(session, run_id, decision.event)
        yield decision.event
    if decision is not None and decision.action == "fail":
        raise decision.error or ValueError("turn-stopping listener failed")


async def correct_invalid_response(
    ctx: Any,
    assembly: AssistantAssembly,
    receipt: ModelCallReceipt,
    rejected_native: bool,
) -> AsyncIterator[Event]:
    reason = "rejected_native_arguments" if rejected_native else None
    if reason is None and assembly.stop_reason == "end_turn":
        text = "".join(b.text for b in assembly.message.content if isinstance(b, TextBlock)).strip()
        if not text:
            reason = "empty_end_turn"
        elif text.startswith("<tool_call>") and text.endswith("</tool_call>"):
            reason = "text_tool_call_envelope"
    if reason is None:
        return
    decision = await ctx.turn_stopping.dispatch(
        TurnStopping(
            ctx.session,
            ctx.run_id,
            ctx.turn_index,
            "invalid_response",
            assembly,
            forced_final=ctx.force_final_pending,
            error_reason=reason,
        )
    )
    if decision.action == "accept":
        return
    async for event in ctx.emit_primary_usage(receipt):
        yield event
    messages = decision.feedback
    if decision.action == "continue" and not rejected_native:
        diagnostic = Message(
            role="assistant", content=[TextBlock(text=INVALID_RESPONSE_DIAGNOSTIC)]
        )
        if (
            len(messages) == 1
            and len(messages[0].content) == 1
            and isinstance(messages[0].content[0], TextBlock)
        ):
            await ctx.ledger.record_projection(
                _projection_record(
                    projection_kind="primary_append",
                    run_id=ctx.run_id,
                    **{key: value for key, value in ctx.active_primary.items() if key != "version"},
                    assistant_sha256=_message_digest(diagnostic),
                    assistant_stop_reason=assembly.stop_reason,
                    after_action="continue",
                    after_feedback=messages[0].content[0].text,
                    control_message_sha256=_message_digest(messages[0]),
                )
            )
        messages = (diagnostic, *messages)
    async for event in append_feedback(
        ctx.session,
        ctx.run_id,
        messages,
        stop_reason=assembly.stop_reason,
        decision=decision,
        boundary=ctx.active_primary,
    ):
        yield event
    await ctx.end_provider_call(stop_reason=assembly.stop_reason, usage=receipt.usage)
    await ctx.save_checkpoint("turn_complete", turn_index=ctx.turn_index)
    ctx.continued = True
