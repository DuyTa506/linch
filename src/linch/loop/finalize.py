"""Terminal-answer finalization for the run loop.

When the model returns a would-be-final answer — either via the terminal
``final_tool`` (structured output) or a plain text response — these helpers run
the closed-loop gates (schema repair, ``BeforeFinalAnswer``/``Stop`` hooks) and
emit the success or error tail. Extracted from ``_run_loop_impl`` so that giant
generator stays focused on the turn loop itself.

The loop drives each via a thin wrapper that supplies the per-turn ``total`` /
``running_cost`` and a :class:`TerminalOutcome`. ``outcome.result`` is set (to
the terminal :class:`RunResultInfo`) only when the run should end; it stays
``None`` when the answer was bounced back for another turn, so the loop reads
the flag to choose between ``return`` and ``continue``."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..events import Event, ResultEvent
from ..observability import RunResultInfo
from ..session import Session
from ..types import AssistantAssembly, ToolUseBlock, Usage
from .checkpoint import _persist_event
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


@dataclass(slots=True)
class FinalizeCtx:
    """Run-scoped dependencies the finalizers need, bundled so the per-call
    signatures stay short. Built once per run by ``_run_loop_impl``."""

    session: Session
    agent: Any
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


@dataclass(slots=True)
class TerminalOutcome:
    """Carries the terminal :class:`RunResultInfo` back to the loop.

    ``result`` is ``None`` while the run should continue (the finalizer bounced
    the answer back for another turn) and set when the run should end."""

    result: RunResultInfo | None = None


def build_run_result(
    *,
    run_id: str,
    session_id: str,
    subtype: str,
    stop_reason: str,
    total: Usage,
    duration_ms: int,
    error: dict[str, Any] | None = None,
) -> RunResultInfo:
    """Build a terminal :class:`RunResultInfo` from current run state.

    Centralises the ``run_id``/``session_id``/``total_usage`` boilerplate
    repeated at every loop exit (success, error, abort, max-turns, finally)."""
    return RunResultInfo(
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
    async for event in _error_result_tail(
        ctx.session,
        ctx.agent,
        run_id=ctx.run_id,
        run_record=ctx.run_record,
        checkpoint=ctx.checkpoint,
        total=total,
        duration_ms=dur,
        running_cost=running_cost,
        final_text_value=final_text_value,
    ):
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
    async for event in _success_result_tail(
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
    ):
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
    raw_structured_output = dict(final_block.input)
    structured_output: dict[str, Any] | None = raw_structured_output
    structured_error: str | None = None
    effective_schema = ctx.opts.output_schema or getattr(ctx.agent, "output_schema", None)
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
            async for event in emit_error_terminal(
                ctx, total=total, running_cost=running_cost, outcome=outcome
            ):
                yield event
            return
        if _gate.decision == "retry":
            async for event in _final_tool_retry_tail(
                ctx.session,
                run_id=ctx.run_id,
                tool_blocks=tool_blocks,
                final_id=final_block.id,
                feedback=_gate.feedback or "",
            ):
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
        async for event in _final_tool_retry_tail(
            ctx.session,
            run_id=ctx.run_id,
            tool_blocks=tool_blocks,
            final_id=final_block.id,
            feedback=feedback or "",
        ):
            yield event
        await ctx.end_active_turn()
        await ctx.save_checkpoint("turn_complete", turn_index=turn_index)
        return
    if hook_action == "stop":
        async for event in emit_error_terminal(
            ctx, total=total, running_cost=running_cost, outcome=outcome, final_text_value=feedback
        ):
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
    )
    proposed, hook_events, stop_action, feedback = await ctx.dispatch_stop(proposed, turn_index)
    for hook_event in hook_events:
        await _persist_event(ctx.session, ctx.run_id, hook_event)
        yield hook_event
    if stop_action == "continue":
        async for event in _final_tool_retry_tail(
            ctx.session,
            run_id=ctx.run_id,
            tool_blocks=tool_blocks,
            final_id=final_block.id,
            feedback=feedback or "",
        ):
            yield event
        await ctx.end_active_turn()
        await ctx.save_checkpoint("turn_complete", turn_index=turn_index)
        return
    if stop_action == "stop":
        async for event in emit_error_terminal(
            ctx,
            total=total,
            running_cost=running_cost,
            outcome=outcome,
            final_text_value=feedback,
            duration_ms=dur,
        ):
            yield event
        return
    async for event in emit_success_terminal(
        ctx, proposed, total=total, running_cost=running_cost, outcome=outcome, duration_ms=dur
    ):
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
    effective_schema = ctx.opts.output_schema or getattr(ctx.agent, "output_schema", None)
    if effective_schema is not None and ft is not None:
        structured_output, structured_error = _parse_structured_output(ft, effective_schema)

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
            async for event in emit_error_terminal(
                ctx,
                total=total,
                running_cost=running_cost,
                outcome=outcome,
                final_text_value=ft,
            ):
                yield event
            return
        if _gate.decision == "retry":
            async for event in _gate_retry_tail(
                ctx.session, run_id=ctx.run_id, feedback=_gate.feedback or ""
            ):
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
        async for event in _gate_retry_tail(
            ctx.session, run_id=ctx.run_id, feedback=feedback or ""
        ):
            yield event
        await ctx.end_active_turn()
        await ctx.save_checkpoint("turn_complete", turn_index=turn_index)
        return
    if hook_action == "stop":
        async for event in emit_error_terminal(
            ctx, total=total, running_cost=running_cost, outcome=outcome, final_text_value=feedback
        ):
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
    )
    proposed, hook_events, stop_action, feedback = await ctx.dispatch_stop(proposed, turn_index)
    for hook_event in hook_events:
        await _persist_event(ctx.session, ctx.run_id, hook_event)
        yield hook_event
    if stop_action == "continue":
        async for event in _gate_retry_tail(
            ctx.session, run_id=ctx.run_id, feedback=feedback or ""
        ):
            yield event
        await ctx.end_active_turn()
        await ctx.save_checkpoint("turn_complete", turn_index=turn_index)
        return
    if stop_action == "stop":
        async for event in emit_error_terminal(
            ctx,
            total=total,
            running_cost=running_cost,
            outcome=outcome,
            final_text_value=feedback,
            duration_ms=dur,
        ):
            yield event
        return
    async for event in emit_success_terminal(
        ctx, proposed, total=total, running_cost=running_cost, outcome=outcome, duration_ms=dur
    ):
        yield event
