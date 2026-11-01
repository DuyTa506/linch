"""Provider-turn phase for the run loop.

The phase owns provider stream assembly and accepted assistant persistence.
It receives run-local callbacks explicitly so the runner remains the only
turn choreographer.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any

from .._prompt_cache import prompt_cache_advisories, tool_signature
from ..compaction import build_compaction_event, reset_read_tracker_after_compaction
from ..context import context_budget_to_dict
from ..errors import ConfigError
from ..events import (
    AssistantEvent,
    CompactionEvent,
    ErrorEvent,
    Event,
    PromptCacheAdvisoryEvent,
)
from ..model_calls import (
    ModelCallReceipt,
    _AccountedAuxiliaryCaller,
    _ModelCallError,
    _ModelCallLedger,
    _projection_record,
)
from ..run_store import LeaseLostError
from ..types import AssistantAssembly, Message, ProviderRequest, Usage
from .checkpoint import _message_digest, _primary_control_message
from .feedback import correct_invalid_response
from .request import _build_context_result, _build_turn_request, _context_selected_tool_names
from .streaming import (
    _AccountedPrimaryAssembly,
    _PrimaryPartial,
    _stream_turn_with_recovery,
    maybe_compact_resilient,
)


@dataclass(slots=True)
class ProviderTurnContext:
    session: Any
    agent: Any
    config: Any
    opts: Any
    turn_index: int
    run_id: str
    signal: Any
    req: ProviderRequest | None
    checkpoint: Any
    ledger: Any
    auxiliary_caller: _AccountedAuxiliaryCaller
    exact_pending_replay: bool
    resumed_assistant: bool
    forced_compactions_used: list[int]
    primary_logical_call_id: str
    forced_compaction_logical_call_id: str
    prepare_provider_call: Callable[..., Awaitable[Any]]
    start_provider_call: Callable[..., Awaitable[None]]
    end_provider_call: Callable[..., Awaitable[None]]
    bind_active_primary: Callable[..., Awaitable[None]]
    save_checkpoint: Callable[..., Awaitable[None]]
    drain_auxiliary_usage: Callable[..., AsyncIterator[Event]]
    emit_primary_usage: Callable[..., AsyncIterator[Event]]
    emit_error_terminal: Callable[..., AsyncIterator[Event]]
    emit_budget_exhausted: Callable[..., AsyncIterator[Event]]
    dispatch_after_provider_call: Callable[..., Awaitable[Any]]
    persist_event: Callable[..., Awaitable[None]]
    flush_events: Callable[..., Awaitable[Any]]
    maybe_save_provider_snapshot: Callable[..., Awaitable[None]]
    close_active_spans: Callable[..., Awaitable[None]]
    sync_ledger_totals: Callable[[], None]
    active_primary: dict[str, Any] | None = None
    budget: Any = None
    force_final_pending: bool = False
    assembly: AssistantAssembly | None = None
    projection: dict[str, Any] | None = None
    control: Message | None = None
    terminal: bool = False
    continued: bool = False
    turn_stopping: Any = None


@dataclass(slots=True)
class ProviderPreparationContext:
    """Dependencies and mutable outcome for one provider-turn preparation."""

    session: Any
    agent: Any
    config: Any
    opts: Any
    turn_index: int
    run_id: str
    signal: Any
    checkpoint: Any
    resume_checkpoint: Any
    start_turn: int
    exact_model_input: bool
    force_final_pending: bool
    forced_final_turn: bool
    recovered_compaction: bool
    recovered_primary_projection: dict[str, Any] | None
    hook_dispatcher_active: bool
    ledger: _ModelCallLedger
    budget: Any
    auxiliary_caller: _AccountedAuxiliaryCaller
    primary_logical_call_id: str
    proactive_compaction_logical_call_id: str
    forced_compaction_logical_call_id: str
    load_pending_provider_request: Callable[[Any], Awaitable[ProviderRequest]]
    dispatch_before_provider_call: Callable[..., Awaitable[Any]]
    persist_event: Callable[[Event], Awaitable[None]]
    drain_auxiliary_usage: Callable[[], AsyncIterator[Event]]
    save_checkpoint: Callable[..., Awaitable[None]]
    emit_budget_exhausted: Callable[..., AsyncIterator[Event]]
    maybe_save_provider_snapshot: Callable[..., Awaitable[None]]
    outcome_req: ProviderRequest | None = None
    outcome_context: Any = None
    outcome_model_override: str | None = None
    resumed_assistant: bool = False
    frozen_pending_resume: bool = False
    exact_pending_replay: bool = False
    provider_action: str | None = None
    provider_feedback: str | None = None
    terminal: bool = False


async def prepare_provider_turn(ctx: ProviderPreparationContext) -> AsyncIterator[Event]:
    """Prepare compaction, context, request, and BeforeProviderCall hooks."""
    pending = ctx.session.pending_skill_overlay
    model_override = None
    if ctx.resume_checkpoint is None or ctx.turn_index != ctx.checkpoint.turn_index:
        ctx.session.current_turn_permission_decisions = {}

    ctx.resumed_assistant = (
        ctx.resume_checkpoint is not None
        and ctx.turn_index == ctx.checkpoint.turn_index
        and ctx.checkpoint.phase
        in {
            "assistant_appended",
            "permission_pending",
            "tool_batch_pending",
            "tool_executing",
            "tool_results_appended",
        }
        and ctx.checkpoint.assistant_message is not None
    )
    if ctx.resumed_assistant:
        # A skill invoked in the preceding tool batch is staged in the
        # checkpoint for the *next* provider turn.  Keep it staged while this
        # resumed turn replays its already-accepted assistant/tool results;
        # consuming it here would silently widen the following turn.
        ctx.session.current_turn_allowed_tools = ctx.checkpoint.current_turn_allowed_tools
    else:
        ctx.session.pending_skill_overlay = None
        if pending is not None:
            ctx.session.current_turn_allowed_tools = pending.allowed_tools
            model_override = pending.model_override
        else:
            ctx.session.current_turn_allowed_tools = None
    ctx.frozen_pending_resume = (
        ctx.exact_model_input
        and ctx.resume_checkpoint is not None
        and ctx.turn_index == ctx.checkpoint.turn_index
        and ctx.checkpoint.phase == "provider_pending"
    )
    ctx.exact_pending_replay = ctx.frozen_pending_resume and not ctx.resumed_assistant
    if ctx.frozen_pending_resume:
        # Replaying a frozen request must retain the checkpointed execution
        # boundary even when the staged skill overlay was already consumed.
        ctx.session.current_turn_allowed_tools = ctx.checkpoint.current_turn_allowed_tools
    if ctx.force_final_pending:
        # The provider request is not the execution boundary: a provider may
        # still emit an unoffered tool call.  Keep the scheduler's availability
        # guard narrowed for this entire turn, including recovery rebuilds.
        ctx.session.current_turn_allowed_tools = []

    compacted = False
    if (
        not ctx.resumed_assistant
        and not ctx.exact_pending_replay
        and not (ctx.recovered_compaction and ctx.turn_index == ctx.start_turn)
    ):
        try:
            compacted = await maybe_compact_resilient(
                ctx.session,
                ctx.agent,
                ctx.signal,
                config=ctx.config,
                model=model_override or ctx.session.active_model or ctx.agent.model,
                auxiliary_caller=ctx.auxiliary_caller,
                logical_call_id=ctx.proactive_compaction_logical_call_id,
            )
        except _ModelCallError as exc:
            async with aclosing(ctx.drain_auxiliary_usage()) as usage_events:
                async for event in usage_events:
                    yield event
            if exc.code == "budget_exceeded" and ctx.budget is not None:
                ctx.terminal = True
                async with aclosing(ctx.emit_budget_exhausted()) as budget_events:
                    async for event in budget_events:
                        yield event
                return
            raise
        async with aclosing(ctx.drain_auxiliary_usage()) as usage_events:
            async for event in usage_events:
                yield event
    if compacted:
        reset_read_tracker_after_compaction(ctx.session, ctx.agent)
        # Skill context reinjection is kept in the request helper to preserve
        # its session mutation order without pulling runner state into this phase.
        from .request import _re_inject_skill_context

        _re_inject_skill_context(ctx.session, config=ctx.config)
        await ctx.maybe_save_provider_snapshot(ctx.session)
        event = build_compaction_event(ctx.session)
        await ctx.persist_event(event)
        yield event

    if not ctx.resumed_assistant and not ctx.exact_pending_replay:
        try:
            ctx.ledger.assert_may_start()
        except _ModelCallError as exc:
            if exc.code == "budget_exceeded" and ctx.budget is not None:
                ctx.terminal = True
                async with aclosing(ctx.emit_budget_exhausted()) as budget_events:
                    async for event in budget_events:
                        yield event
                return
            raise

    context_result = (
        None
        if ctx.resumed_assistant or ctx.frozen_pending_resume
        else await _build_context_result(ctx.session, ctx.turn_index, config=ctx.config)
    )
    if context_result is not None:
        from ..events import ContextBuildEvent

        event = ContextBuildEvent(
            system_blocks=len(context_result.system_blocks),
            messages=len(context_result.messages),
            selected_tools=_context_selected_tool_names(context_result),
            budget=context_budget_to_dict(context_result.budget),
            metadata=dict(context_result.metadata),
        )
        await ctx.persist_event(event)
        yield event

    if ctx.resumed_assistant:
        req = None
    elif ctx.exact_pending_replay:
        req = await ctx.load_pending_provider_request(ctx.signal)
    else:
        req = _build_turn_request(
            ctx.session,
            ctx.opts,
            config=ctx.config,
            context=context_result,
            model_override=model_override,
        )
    if req is not None and ctx.force_final_pending and not ctx.exact_pending_replay:
        req.tools = []
        req.tool_choice = None

    ctx.outcome_req = req
    ctx.outcome_context = context_result
    ctx.outcome_model_override = model_override


async def run_provider_turn(ctx: ProviderTurnContext) -> AsyncIterator[Event]:
    """Stream and commit one provider response, preserving yield order."""
    session = ctx.session
    agent = ctx.agent
    opts = ctx.opts
    turn_index = ctx.turn_index
    signal = ctx.signal
    req = ctx.req
    checkpoint = ctx.checkpoint
    ledger = ctx.ledger
    auxiliary_caller = ctx.auxiliary_caller
    exact_pending_replay = ctx.exact_pending_replay
    resumed_assistant = ctx.resumed_assistant
    forced_compactions_used = ctx.forced_compactions_used
    primary_logical_call_id = ctx.primary_logical_call_id
    forced_compaction_logical_call_id = ctx.forced_compaction_logical_call_id

    assembly: AssistantAssembly | None = None
    primary_receipt: ModelCallReceipt | None = None
    primary_rejected_native = False
    staged_partials: dict[str, list[Any]] = {}
    accepted_partials: list[Any] = []
    projection: dict[str, Any] | None = None
    control: Message | None = None
    if resumed_assistant:
        assert checkpoint.assistant_message is not None
        assembly = AssistantAssembly(
            message=checkpoint.assistant_message,
            stop_reason=checkpoint.assistant_stop_reason or "tool_use",
            usage=Usage(),
        )
    else:
        assert req is not None
        _sig = tool_signature(req.tools)
        for _reason, _detail in prompt_cache_advisories(
            prev_model=session._last_prompt_cache_model,
            prev_signature=session._last_prompt_cache_tool_sig,
            model=req.model,
            signature=_sig,
        ):
            _adv: Event = PromptCacheAdvisoryEvent(reason=_reason, detail=_detail)
            await ctx.persist_event(_adv)
            yield _adv
        session._last_prompt_cache_model = req.model
        session._last_prompt_cache_tool_sig = _sig
        _ladder = getattr(agent, "compaction_ladder", None)
        _provider_stream = _stream_turn_with_recovery(
            session,
            agent,
            opts,
            req,
            turn_index=turn_index,
            signal=signal,
            ladder=_ladder,
            forced_used=forced_compactions_used,
            prepare_provider_call=ctx.prepare_provider_call,
            start_provider_call=ctx.start_provider_call,
            end_provider_call=ctx.end_provider_call,
            request_prepared=exact_pending_replay,
            ledger=ledger,
            logical_call_id=primary_logical_call_id,
            bind_active_primary=ctx.bind_active_primary,
            auxiliary_caller=auxiliary_caller,
            compaction_logical_call_id=forced_compaction_logical_call_id,
            config=ctx.config,
        )
        try:
            async with aclosing(_provider_stream) as provider_stream:
                async for item in provider_stream:
                    if isinstance(item, _PrimaryPartial):
                        if item.delivery_attempt_id is not None:
                            staged_partials.setdefault(item.delivery_attempt_id, []).append(
                                item.event
                            )
                        yield item.event
                    elif isinstance(item, _AccountedPrimaryAssembly):
                        assembly = item.assembly
                        primary_receipt = item.receipt
                        primary_rejected_native = item.rejected_native
                        accepted_partials = staged_partials.pop(
                            item.receipt.delivery_attempt_id, []
                        )
                    elif isinstance(item, AssistantAssembly):
                        assembly = item
                    else:
                        if isinstance(item, CompactionEvent):
                            async with aclosing(ctx.drain_auxiliary_usage()) as usage_events:
                                async for usage_event in usage_events:
                                    yield usage_event
                            await ctx.save_checkpoint(checkpoint.phase, turn_index=turn_index)
                            ledger.assert_may_start()
                            await ctx.maybe_save_provider_snapshot(session)
                        await ctx.persist_event(item)
                        yield item
        except LeaseLostError:
            raise
        except Exception as exc:
            async with aclosing(ctx.drain_auxiliary_usage()) as usage_events:
                async for usage_event in usage_events:
                    yield usage_event
            if (
                isinstance(exc, _ModelCallError)
                and exc.code == "budget_exceeded"
                and getattr(ctx, "budget", None) is not None
            ):
                ctx.terminal = True
                await ctx.close_active_spans(stop_reason="error")
                async with aclosing(ctx.emit_budget_exhausted()) as budget_events:
                    async for event in budget_events:
                        yield event
                return
            raise

    if assembly is None:
        raise RuntimeError("provider stream ended without assistant assembly")

    if not resumed_assistant:
        if primary_receipt is None or primary_receipt.usage is None:
            raise RuntimeError("accounted primary stream ended without a reported receipt")
        _provider_usage = primary_receipt.usage
        ctx.sync_ledger_totals()
        if ctx.turn_stopping.active:
            async with aclosing(
                correct_invalid_response(ctx, assembly, primary_receipt, primary_rejected_native)
            ) as correction_events:
                async for event in correction_events:
                    yield event
            if ctx.continued:
                return
        if primary_rejected_native:
            ctx.terminal = True
            await ctx.save_checkpoint("provider_pending", turn_index=turn_index)
            async with aclosing(ctx.emit_primary_usage(primary_receipt)) as usage_events:
                async for event in usage_events:
                    yield event
            event = ErrorEvent(
                error={
                    "name": "ProviderError",
                    "message": "provider response contained malformed native tool arguments",
                    "retryable": False,
                }
            )
            await ctx.persist_event(event)
            yield event
            async with aclosing(
                ctx.emit_error_terminal(end_provider_call_usage=_provider_usage)
            ) as terminal_events:
                async for event in terminal_events:
                    yield event
            return
        for partial_event in accepted_partials:
            await ctx.persist_event(partial_event)

        (
            assembly,
            hook_events,
            after_action,
            after_feedback,
        ) = await ctx.dispatch_after_provider_call(assembly, turn_index)
        assert assembly is not None
        for hook_event in hook_events:
            await ctx.persist_event(hook_event)
            yield hook_event
        if ctx.active_primary is None:
            raise ConfigError("accepted primary response lacks its active binding")
        control = (
            _primary_control_message(assembly.message, after_feedback)
            if after_action == "continue"
            else None
        )
        projection = _projection_record(
            projection_kind="primary_append",
            run_id=ctx.run_id,
            **{key: value for key, value in ctx.active_primary.items() if key != "version"},
            assistant_sha256=_message_digest(assembly.message),
            assistant_stop_reason=assembly.stop_reason,
            after_action=after_action,
            after_feedback=after_feedback,
            control_message_sha256=_message_digest(control) if control is not None else None,
        )
        await ledger.record_projection(projection)
        if after_action == "stop":
            ctx.assembly = assembly
            ctx.projection = projection
            ctx.terminal = True
            async with aclosing(ctx.emit_primary_usage(primary_receipt)) as usage_events:
                async for event in usage_events:
                    yield event
            async with aclosing(
                ctx.emit_error_terminal(
                    final_text_value=after_feedback,
                    end_provider_call_usage=_provider_usage,
                )
            ) as terminal_events:
                async for event in terminal_events:
                    yield event
            return
        await ctx.end_provider_call(stop_reason=assembly.stop_reason, usage=_provider_usage)
        await session.append([assembly.message])
        ctx.force_final_pending = False
        await ctx.save_checkpoint(
            "assistant_appended",
            turn_index=turn_index,
            assistant_message=assembly.message,
            assistant_stop_reason=assembly.stop_reason,
        )
        event = AssistantEvent(message=assembly.message, stop_reason=assembly.stop_reason)
        await ctx.persist_event(event)
        yield event
        async with aclosing(ctx.emit_primary_usage(primary_receipt)) as usage_events:
            async for event in usage_events:
                yield event

    ctx.assembly = assembly
    ctx.projection = projection if not resumed_assistant else None
    ctx.control = control if not resumed_assistant else None
