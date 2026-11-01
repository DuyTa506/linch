"""Terminal-answer finalization for the run loop.

When the model returns a would-be-final answer — either via the terminal
``final_tool`` (structured output) or a plain text response — these helpers run
the closed-loop gates (schema repair, ``BeforeFinalAnswer``/``Stop`` hooks) and
emit the success or error tail. Extracted from ``_run_loop_impl`` so that giant
generator stays focused on the turn loop itself.

The loop drives each via a thin wrapper that supplies the per-turn ``total`` /
``running_cost`` and a :class:`TerminalOutcome`. ``outcome.result`` is set (to
the terminal :class:`_AgentResult`) only when the run should end; it stays
``None`` when the answer was bounced back for another turn, so the loop reads
the flag to choose between ``return`` and ``continue``."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any

from ..errors import ProviderError
from ..events import Event, ResultEvent
from ..hooks.contexts import _AgentResult
from ..session import Session
from ..types import AssistantAssembly, Message, TextBlock, ToolUseBlock, Usage
from .checkpoint import _message_digest, _persist_event, _primary_control_message
from .feedback import append_feedback
from .request import final_text
from .terminals import (
    _error_result_tail,
    _evaluate_terminal_gates,
    _final_tool_retry_tail,
    _gate_retry_tail,
    _parse_structured_output,
    _success_result_tail,
    _validate_structured_output,
)
from .turn_stopping import TurnStopping


@dataclass(slots=True)
class FinalizeCtx:
    """Run-scoped dependencies the finalizers need, bundled so the per-call
    signatures stay short. Built once per run by ``_run_loop_impl``."""

    session: Session
    runtime: Any
    agent: Any
    config: Any
    run_id: str
    run_record: Any
    checkpoint: Any
    started: float
    opts: Any
    effective_final_tool: str | None
    max_schema_retries: int
    max_final_answer_reentries: int
    gate_attempts: list[int]
    final_answer_reentries: list[int]
    save_checkpoint: Callable[..., Awaitable[None]]
    end_active_turn: Callable[[], Awaitable[None]]
    end_active_provider_call: Callable[..., Awaitable[None]]
    dispatch_before_final_answer: Callable[..., Awaitable[Any]]
    dispatch_stop: Callable[..., Awaitable[Any]]
    usage_complete: Callable[[], bool]
    turn_stopping: Any


@dataclass(slots=True)
class TerminalOutcome:
    """Carries the terminal :class:`_AgentResult` back to the loop.

    ``result`` is ``None`` while the run should continue (the finalizer bounced
    the answer back for another turn) and set when the run should end."""

    result: _AgentResult | None = None


@dataclass(slots=True)
class RunFinalization:
    """Run-bound adapters for the terminal-answer phase.

    ``FinalizeCtx`` contains the hook and checkpoint dependencies specific to
    answer gates.  This adapter supplies the shared ledger/runtime state and
    keeps those details out of the turn choreography in ``runner.py``.
    """

    ctx: FinalizeCtx
    runtime: Any
    result: _AgentResult | None = None

    async def emit_error(
        self,
        *,
        final_text_value: str | None = None,
        duration_ms: int | None = None,
        end_provider_call_usage: Usage | None = None,
    ) -> AsyncIterator[Event]:
        self.runtime.sync_checkpoint_accounting()
        outcome = TerminalOutcome()
        async with aclosing(
            emit_error_terminal(
                self.ctx,
                total=self.runtime.total,
                running_cost=self.runtime.running_cost,
                outcome=outcome,
                final_text_value=final_text_value,
                duration_ms=duration_ms,
                end_provider_call_usage=end_provider_call_usage,
            )
        ) as error_events:
            async for event in error_events:
                yield event
        self.result = outcome.result

    async def finalize(
        self, turn_index: int, forced_final_turn: bool, **answer: Any
    ) -> AsyncIterator[Event]:
        forced = forced_final_turn
        tool_blocks = answer.get("tool_blocks", ())
        final_block = answer.get("final_block")
        candidate = TurnStopping(
            self.ctx.session,
            self.ctx.run_id,
            turn_index,
            "forced_final" if forced else "final_tool" if final_block else "final_text",
            answer.get("assembly"),
            tuple(tool_blocks),
            final_block,
            forced,
        )
        decision = await self.ctx.turn_stopping.dispatch(candidate)
        messages = decision.feedback
        if final_block is not None and decision.action != "accept":
            paired = _primary_control_message(
                Message(role="assistant", content=list(tool_blocks)),
                "Final tool was not executed.",
                final_block.id,
            )
            messages = (paired, *messages)
        replayed = self.runtime.replayed_feedback
        if (
            replayed
            and decision.action != "accept"
            and list(map(_message_digest, replayed)) != list(map(_message_digest, messages))
        ):
            raise ValueError("replayed turn feedback differs from its decision")
        self.runtime.replayed_feedback = ()
        if decision.action != "accept":
            async with aclosing(
                append_feedback(
                    self.ctx.session,
                    self.ctx.run_id,
                    messages,
                    paired=bool(tool_blocks),
                    decision=decision,
                )
            ) as feedback_events:
                async for event in feedback_events:
                    yield event
            await self.ctx.end_active_turn()
            await self.ctx.save_checkpoint("turn_complete", turn_index=turn_index)
            return
        self.runtime.sync_checkpoint_accounting()
        outcome = TerminalOutcome()
        finalize = finalize_final_tool_answer if final_block else finalize_text_answer
        async with aclosing(
            finalize(
                self.ctx,
                turn_index=turn_index,
                **answer,
                total=self.runtime.total,
                running_cost=self.runtime.running_cost,
                forced_final_turn=forced,
                outcome=outcome,
            )
        ) as final_events:
            async for event in final_events:
                yield event
        self.result = outcome.result


def assembly_with_truncation_prefix(assembly: AssistantAssembly, prefix: str) -> AssistantAssembly:
    """Prepend recovered truncated text to a terminal assistant assembly."""
    if not prefix:
        return assembly
    text = final_text(assembly.message)
    if text is None:
        return assembly
    merged = Message(
        role="assistant",
        content=[TextBlock(text=f"{prefix}{text}")],
        provider_metadata=assembly.message.provider_metadata,
    )
    return AssistantAssembly(message=merged, stop_reason=assembly.stop_reason, usage=assembly.usage)


def last_user_message_is_feedback(session: Session, feedback: str) -> bool:
    """Return whether the current provider history already contains feedback."""
    from ..skills.system_reminder import wrap_in_system_reminder

    message = session.last_provider_message()
    if message is None or message.role != "user":
        return False
    expected = wrap_in_system_reminder(feedback)
    return any(isinstance(block, TextBlock) and block.text == expected for block in message.content)


def build_run_result(
    *,
    run_id: str,
    session_id: str,
    subtype: str,
    stop_reason: str,
    total: Usage,
    duration_ms: int,
    error: dict[str, Any] | None = None,
) -> _AgentResult:
    """Build a terminal :class:`_AgentResult` from current run state.

    Centralises the ``run_id``/``session_id``/``total_usage`` boilerplate
    repeated at every loop exit (success, error, abort, max-turns, finally)."""
    return _AgentResult(
        run_id=run_id,
        session_id=session_id,
        subtype=subtype,
        stop_reason=stop_reason,
        total_usage=total,
        duration_ms=duration_ms,
        error=error,
    )


async def emit_error_terminal(
    ctx: FinalizeCtx,
    *,
    total: Usage,
    running_cost: float | None,
    outcome: TerminalOutcome,
    final_text_value: str | None = None,
    duration_ms: int | None = None,
    end_provider_call_usage: Usage | None = None,
) -> AsyncIterator[Event]:
    """Record the error result on *outcome*, close spans, and emit the error tail.

    ``duration_ms`` defaults to a fresh wall-clock reading; pass it explicitly
    when the caller reused a ``_dur`` computed earlier. ``end_provider_call_usage``
    closes the active provider-call span first (the AfterProviderCall stop path)."""
    dur = duration_ms if duration_ms is not None else int((time.time() - ctx.started) * 1000)
    outcome.result = build_run_result(
        run_id=ctx.run_id,
        session_id=ctx.session.id,
        subtype="error",
        stop_reason="error",
        total=total,
        duration_ms=dur,
    )
    if end_provider_call_usage is not None:
        await ctx.end_active_provider_call(stop_reason="error", usage=end_provider_call_usage)
    await ctx.end_active_turn()
    async with aclosing(
        _error_result_tail(
            ctx.session,
            runtime=ctx.runtime,
            run_id=ctx.run_id,
            total=total,
            duration_ms=dur,
            running_cost=running_cost,
            final_text_value=final_text_value,
            usage_complete=ctx.usage_complete(),
        )
    ) as error_events:
        async for event in error_events:
            yield event


async def emit_success_terminal(
    ctx: FinalizeCtx,
    proposed: ResultEvent,
    *,
    total: Usage,
    running_cost: float | None,
    outcome: TerminalOutcome,
    duration_ms: int,
) -> AsyncIterator[Event]:
    """Record the success result on *outcome*, close the turn span, and emit the
    success tail from a hook-approved ``proposed`` ResultEvent."""
    outcome.result = build_run_result(
        run_id=ctx.run_id,
        session_id=ctx.session.id,
        subtype=proposed.subtype,
        stop_reason=proposed.stop_reason,
        total=total,
        duration_ms=duration_ms,
    )
    await ctx.end_active_turn()
    async with aclosing(
        _success_result_tail(
            ctx.session,
            ctx.agent,
            run_id=ctx.run_id,
            run_record=ctx.run_record,
            checkpoint=ctx.checkpoint,
            total=total,
            duration_ms=duration_ms,
            running_cost=running_cost,
            stop_reason=proposed.stop_reason,
            final_text_value=proposed.final_text,
            structured_output=proposed.structured_output,
            structured_error=proposed.structured_error,
            usage_complete=ctx.usage_complete(),
        )
    ) as success_events:
        async for event in success_events:
            yield event


async def finalize_final_tool_answer(
    ctx: FinalizeCtx,
    *,
    turn_index: int,
    tool_blocks: list[ToolUseBlock],
    final_block: ToolUseBlock,
    total: Usage,
    running_cost: float | None,
    forced_final_turn: bool,
    outcome: TerminalOutcome,
) -> AsyncIterator[Event]:
    """Terminal final-tool path: treat the tool input as structured output, run
    the schema/verifier/stop gates, then emit the success or error tail."""
    if len(tool_blocks) != 1:
        feedback = (
            "Invalid terminal tool batch: the final-answer tool must be the only "
            "tool call in its assistant message; no calls were executed."
        )
        async with aclosing(
            _final_tool_retry_tail(
                ctx.session,
                run_id=ctx.run_id,
                tool_blocks=tool_blocks,
                final_id=final_block.id,
                feedback=feedback,
            )
        ) as retry_events:
            async for event in retry_events:
                yield event
        raise ProviderError(feedback)

    # A terminal tool is intercepted rather than scheduled, but provider
    # histories still require every assistant tool_use to have a matching
    # user tool_result.  Persist the acknowledgement before any terminal gate
    # so success, stop, and crash paths all leave a reusable session.
    async with aclosing(
        _final_tool_retry_tail(
            ctx.session,
            run_id=ctx.run_id,
            tool_blocks=tool_blocks,
            final_id=final_block.id,
            feedback="Final answer captured by the host.",
            final_is_error=False,
        )
    ) as ack_events:
        async for event in ack_events:
            yield event

    raw_structured_output = dict(final_block.input)
    structured_output: dict[str, Any] | None = raw_structured_output
    structured_error: str | None = None
    effective_schema = ctx.opts.output_schema or ctx.config.output_schema
    if effective_schema is not None:
        structured_error = _validate_structured_output(raw_structured_output, effective_schema)
        if structured_error is not None:
            structured_output = None
    if not forced_final_turn and ctx.max_schema_retries:
        _gate = await _evaluate_terminal_gates(
            ctx.session,
            run_id=ctx.run_id,
            max_schema_retries=ctx.max_schema_retries,
            attempts=ctx.gate_attempts,
            structured_output=structured_output,
            structured_error=structured_error,
        )
        for event in _gate.events:
            yield event
        if _gate.decision == "stop":
            async with aclosing(
                emit_error_terminal(ctx, total=total, running_cost=running_cost, outcome=outcome)
            ) as error_events:
                async for event in error_events:
                    yield event
            return
        if _gate.decision == "retry":
            async with aclosing(
                _gate_retry_tail(ctx.session, run_id=ctx.run_id, feedback=_gate.feedback or "")
            ) as retry_events:
                async for event in retry_events:
                    yield event
            await ctx.end_active_turn()
            await ctx.save_checkpoint("turn_complete", turn_index=turn_index)
            return

    (
        ft,
        structured_output,
        structured_error,
        hook_events,
        hook_action,
        feedback,
    ) = await ctx.dispatch_before_final_answer(
        turn_index=turn_index,
        final_text_value=None,
        structured_output=structured_output,
        structured_error=structured_error,
        stop_reason="tool_use",
        final_tool_name=ctx.effective_final_tool,
        tool_use=final_block,
        skip=forced_final_turn,
    )
    for hook_event in hook_events:
        await _persist_event(ctx.session, ctx.run_id, hook_event)
        yield hook_event
    if hook_action == "retry" and ctx.final_answer_reentries[0] < ctx.max_final_answer_reentries:
        ctx.final_answer_reentries[0] += 1
        async with aclosing(
            _gate_retry_tail(ctx.session, run_id=ctx.run_id, feedback=feedback or "")
        ) as retry_events:
            async for event in retry_events:
                yield event
        await ctx.end_active_turn()
        await ctx.save_checkpoint("turn_complete", turn_index=turn_index)
        return
    if hook_action == "stop":
        async with aclosing(
            emit_error_terminal(
                ctx,
                total=total,
                running_cost=running_cost,
                outcome=outcome,
                final_text_value=feedback,
            )
        ) as error_events:
            async for event in error_events:
                yield event
        return
    dur = int((time.time() - ctx.started) * 1000)
    proposed = ResultEvent(
        subtype="success",
        stop_reason="tool_use",
        total_usage=total,
        duration_ms=dur,
        final_text=ft,
        structured_output=structured_output,
        structured_error=structured_error,
        total_cost_usd=running_cost,
        usage_complete=ctx.usage_complete(),
    )
    proposed, hook_events, stop_action, feedback = await ctx.dispatch_stop(proposed, turn_index)
    for hook_event in hook_events:
        await _persist_event(ctx.session, ctx.run_id, hook_event)
        yield hook_event
    if stop_action == "continue":
        async with aclosing(
            _gate_retry_tail(ctx.session, run_id=ctx.run_id, feedback=feedback or "")
        ) as retry_events:
            async for event in retry_events:
                yield event
        await ctx.end_active_turn()
        await ctx.save_checkpoint("turn_complete", turn_index=turn_index)
        return
    if stop_action == "stop":
        async with aclosing(
            emit_error_terminal(
                ctx,
                total=total,
                running_cost=running_cost,
                outcome=outcome,
                final_text_value=feedback,
                duration_ms=dur,
            )
        ) as error_events:
            async for event in error_events:
                yield event
        return
    async with aclosing(
        emit_success_terminal(
            ctx,
            proposed,
            total=total,
            running_cost=running_cost,
            outcome=outcome,
            duration_ms=dur,
        )
    ) as success_events:
        async for event in success_events:
            yield event


async def finalize_text_answer(
    ctx: FinalizeCtx,
    *,
    turn_index: int,
    assembly: AssistantAssembly,
    total: Usage,
    running_cost: float | None,
    forced_final_turn: bool,
    outcome: TerminalOutcome,
) -> AsyncIterator[Event]:
    """Terminal plain-text path: parse/validate structured output, run the
    schema/verifier/stop gates, then emit the success or error tail."""
    ft = final_text(assembly.message)
    structured_output: dict[str, Any] | None = None
    structured_error: str | None = None
    effective_schema = ctx.opts.output_schema or ctx.config.output_schema
    if effective_schema is not None:
        # Empty assistant content is still an attempted structured response;
        # surface/repair its parse error instead of silently returning success
        # with ``structured_output=None`` and no explanation.
        structured_output, structured_error = _parse_structured_output(ft or "", effective_schema)

    # ── Closed-loop gates: schema repair, then verifiers ──────
    # Skipped on a loop-guard force_final turn: a guard-tripped run must not
    # be bounced back into the loop.
    if not forced_final_turn and ctx.max_schema_retries:
        _gate = await _evaluate_terminal_gates(
            ctx.session,
            run_id=ctx.run_id,
            max_schema_retries=ctx.max_schema_retries,
            attempts=ctx.gate_attempts,
            structured_output=structured_output,
            structured_error=structured_error,
        )
        for event in _gate.events:
            yield event
        if _gate.decision == "stop":
            async with aclosing(
                emit_error_terminal(
                    ctx,
                    total=total,
                    running_cost=running_cost,
                    outcome=outcome,
                    final_text_value=ft,
                )
            ) as error_events:
                async for event in error_events:
                    yield event
            return
        if _gate.decision == "retry":
            async with aclosing(
                _gate_retry_tail(ctx.session, run_id=ctx.run_id, feedback=_gate.feedback or "")
            ) as retry_events:
                async for event in retry_events:
                    yield event
            await ctx.end_active_turn()
            await ctx.save_checkpoint("turn_complete", turn_index=turn_index)
            return

    (
        ft,
        structured_output,
        structured_error,
        hook_events,
        hook_action,
        feedback,
    ) = await ctx.dispatch_before_final_answer(
        turn_index=turn_index,
        final_text_value=ft,
        structured_output=structured_output,
        structured_error=structured_error,
        stop_reason=assembly.stop_reason,
        skip=forced_final_turn,
    )
    for hook_event in hook_events:
        await _persist_event(ctx.session, ctx.run_id, hook_event)
        yield hook_event
    if hook_action == "retry" and ctx.final_answer_reentries[0] < ctx.max_final_answer_reentries:
        ctx.final_answer_reentries[0] += 1
        async with aclosing(
            _gate_retry_tail(ctx.session, run_id=ctx.run_id, feedback=feedback or "")
        ) as retry_events:
            async for event in retry_events:
                yield event
        await ctx.end_active_turn()
        await ctx.save_checkpoint("turn_complete", turn_index=turn_index)
        return
    if hook_action == "stop":
        async with aclosing(
            emit_error_terminal(
                ctx,
                total=total,
                running_cost=running_cost,
                outcome=outcome,
                final_text_value=feedback,
            )
        ) as error_events:
            async for event in error_events:
                yield event
        return

    dur = int((time.time() - ctx.started) * 1000)
    proposed = ResultEvent(
        subtype="success",
        stop_reason=assembly.stop_reason,
        total_usage=total,
        duration_ms=dur,
        final_text=ft,
        structured_output=structured_output,
        structured_error=structured_error,
        total_cost_usd=running_cost,
        usage_complete=ctx.usage_complete(),
    )
    proposed, hook_events, stop_action, feedback = await ctx.dispatch_stop(proposed, turn_index)
    for hook_event in hook_events:
        await _persist_event(ctx.session, ctx.run_id, hook_event)
        yield hook_event
    if stop_action == "continue":
        async with aclosing(
            _gate_retry_tail(ctx.session, run_id=ctx.run_id, feedback=feedback or "")
        ) as retry_events:
            async for event in retry_events:
                yield event
        await ctx.end_active_turn()
        await ctx.save_checkpoint("turn_complete", turn_index=turn_index)
        return
    if stop_action == "stop":
        async with aclosing(
            emit_error_terminal(
                ctx,
                total=total,
                running_cost=running_cost,
                outcome=outcome,
                final_text_value=feedback,
                duration_ms=dur,
            )
        ) as error_events:
            async for event in error_events:
                yield event
        return
    async with aclosing(
        emit_success_terminal(
            ctx,
            proposed,
            total=total,
            running_cost=running_cost,
            outcome=outcome,
            duration_ms=dur,
        )
    ) as success_events:
        async for event in success_events:
            yield event
