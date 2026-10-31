"""The agent run loop: ``run_loop`` / ``resume_loop`` entry points and the
turn-by-turn ``_run_loop_impl`` generator that drives provider calls, tool
execution, guards, gates, budgets, and durable checkpoints."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Any
from uuid import uuid4

from .._telemetry import store_call, trace_run
from .._wire import UnsupportedWireVersionError
from ..errors import AbortError, ConfigError, RunNotResumableError
from ..events import Event, ResultEvent, UserEvent
from ..hooks import (
    AgentStopContext,
    HookEvent,
)
from ..hooks.contexts import _AgentResult
from ..model_calls import (
    ModelCallIntent,
    _AccountedAuxiliaryCaller,
)
from ..run_store import (
    LeaseLostError,
    RunCheckpoint,
    RunContractMismatchError,
    RunRecord,
    _require_tool_recovery_cursor,
    ensure_run_contract_compatible,
    run_contract_from_meta,
    run_meta_with_contract,
)
from ..session import RunOptions, Session
from ..types import (
    AssistantAssembly,
    Message,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from .checkpoint import (
    _alignment_entries_from_checkpoint,
    _flush_events,
    _message_digest,
    _persist_event,
    _primary_control_message,
    _skill_overlay_from_dict,
)
from .contracts import (
    _build_run_contract,
    _effective_final_tool_name,
    _exact_model_input_enabled,
)
from .finalize import (
    FinalizeCtx,
    RunFinalization,
    assembly_with_truncation_prefix,
    last_user_message_is_feedback,
)
from .provider_turn import (
    ProviderPreparationContext,
    ProviderTurnContext,
    prepare_provider_turn,
    run_provider_turn,
)
from .request import (
    final_text,
)
from .resume import ResumeContext, ResumeState, recover_resume
from .runtime import RunRuntime
from .runtime import (
    TurnLifecycle as _TurnLifecycle,
)
from .runtime import (
    checkpoint_extension_state as _checkpoint_extension_state,
)
from .runtime import (
    checkpointable_hooks as _checkpointable_hooks,
)
from .runtime import (
    drain_alignment as _drain_alignment,
)
from .runtime import (
    drain_child_events as _drain_child_events,
)
from .runtime import (
    drain_mailbox as _drain_mailbox,
)
from .runtime import (
    drain_pending_notifications as _drain_pending_notifications,
)
from .runtime import (
    maybe_save_provider_snapshot as _maybe_save_provider_snapshot,
)
from .runtime import (
    require_model_input_snapshot_store as _require_model_input_snapshot_store,
)
from .runtime import (
    restore_checkpointable_hooks as _restore_checkpointable_hooks,
)
from .startup import RunStartupContext, initialize_run
from .terminals import (
    _complete_with_pending_events,
    _final_tool_retry_tail,
    _gate_retry_tail,
    _max_turns_tail,
    _stop_when_tail,
)
from .tool_batch import ToolBatchContext, run_tool_batch

# Max BeforeFinalAnswer-induced retries honored per run before the answer is
# accepted as-is. Keeps a perpetually-blocking final-answer hook from looping.
_MAX_FINAL_ANSWER_REENTRIES = 1


async def run_loop(session: Session, prompt: str, opts: RunOptions) -> AsyncIterator[Event]:
    if _exact_model_input_enabled(session.agent):
        _require_model_input_snapshot_store(session.agent)
    await session.agent._ensure_provider_prepared()
    store = session.agent.run_store
    run_id = opts.run_id or str(uuid4())
    session.active_run_id = run_id
    session.run_deps = opts.deps if opts.deps is not None else getattr(session.agent, "deps", None)
    run_record = (
        await store_call(
            session,
            "run.create_run",
            store.create_run(
                session.id,
                id=run_id,
                meta=run_meta_with_contract(_build_run_contract(session, prompt, opts)),
                initial_checkpoint=RunCheckpoint(
                    phase="started",
                    prompt=prompt,
                    turn_index=0,
                    total_usage=Usage(),
                    extension_state={
                        **_checkpoint_extension_state(
                            _checkpointable_hooks(list(session.agent.hooks or [])), session, run_id
                        ),
                        "linch.startup": {
                            "preparation_required": any(
                                callable(getattr(hook, name, None))
                                for hook in session.agent.hooks or ()
                                for name in (
                                    "on_hook",
                                    "on_agent_start",
                                    "on_user_prompt_submit",
                                    "on_event_emit",
                                )
                            ),
                            "history_count": session.session_log.history_count,
                            "history_fingerprint": session.session_log._history_fingerprint(),
                        },
                    },
                ),
            ),
        )
        if store is not None
        else None
    )
    run_id = run_record.id if run_record is not None else run_id
    async with aclosing(
        _run_loop_impl(
            session,
            prompt,
            opts,
            run_id=run_id,
            run_record=run_record,
            resume_checkpoint=None,
        )
    ) as iterator:
        async for event in iterator:
            yield event


async def resume_loop(session: Session, run_id: str, opts: RunOptions) -> AsyncIterator[Event]:
    if _exact_model_input_enabled(session.agent):
        _require_model_input_snapshot_store(session.agent)
    await session.agent._ensure_provider_prepared()
    store = session.agent.run_store
    if store is None:
        raise RuntimeError("Agent has no run_store configured")
    run_record = await store_call(session, "run.load_run", store.load_run(run_id))
    if run_record is None:
        raise KeyError(f"run not found: {run_id}")
    if run_record.session_id != session.id:
        raise ValueError(f"run {run_id} belongs to session {run_record.session_id}")
    if run_record.status in {"completed", "failed", "aborted"}:
        raise RunNotResumableError(run_id, run_record.status)
    checkpoint = run_record.checkpoint
    if checkpoint is None:
        raise RunNotResumableError(run_id, run_record.status)
    requested_contract = _build_run_contract(session, checkpoint.prompt, opts)
    try:
        stored_contract = run_contract_from_meta(run_record.meta)
        ensure_run_contract_compatible(
            stored_contract,
            requested_contract,
        )
        if checkpoint.phase in {
            "tool_batch_pending",
            "permission_pending",
            "tool_executing",
            "tool_results_appended",
        } or (
            checkpoint.phase == "assistant_appended"
            and checkpoint.assistant_message is not None
            and any(
                isinstance(block, ToolUseBlock) for block in checkpoint.assistant_message.content
            )
        ):
            _require_tool_recovery_cursor(checkpoint.tool_batch_event_after_seq)
    except UnsupportedWireVersionError:
        raise
    except (RunContractMismatchError, TypeError, ValueError) as exc:
        raise ConfigError(f"Cannot safely resume run {run_id}: {exc}") from None
    async with aclosing(
        _run_loop_impl(
            session,
            checkpoint.prompt,
            opts,
            run_id=run_record.id,
            run_record=run_record,
            resume_checkpoint=checkpoint,
        )
    ) as iterator:
        async for event in iterator:
            yield event


@trace_run
async def _run_loop_impl(  # pyright: ignore[reportGeneralTypeIssues]
    session: Session,
    prompt: str,
    opts: RunOptions,
    *,
    run_id: str,
    run_record: RunRecord | None,
    resume_checkpoint: RunCheckpoint | None,
) -> AsyncIterator[Event]:
    from ..loop_guard import LoopGuardState

    _runtime = await RunRuntime.create(
        session,
        prompt,
        opts,
        run_id=run_id,
        run_record=run_record,
        resume_checkpoint=resume_checkpoint,
        exact_model_input_enabled=_exact_model_input_enabled,
        require_model_input_snapshot_store=_require_model_input_snapshot_store,
        checkpointable_hooks=_checkpointable_hooks,
        restore_checkpointable_hooks=_restore_checkpointable_hooks,
        checkpoint_extension_state=_checkpoint_extension_state,
        effective_final_tool_name=_effective_final_tool_name,
        loop_guard_state_factory=LoopGuardState,
        skill_overlay_from_dict=_skill_overlay_from_dict,
        alignment_entries_from_checkpoint=_alignment_entries_from_checkpoint,
    )
    agent = _runtime.agent
    _exact_model_input = _runtime.exact_model_input
    started = _runtime.started
    total = _runtime.total
    running_cost = _runtime.running_cost
    _forced_compactions_used = _runtime.forced_compactions_used
    hook_dispatcher = _runtime.hook_dispatcher
    _dispatch_user_prompt = _runtime.dispatch_user_prompt
    _dispatch_lifecycle = _runtime.dispatch_lifecycle
    _dispatch_before_provider_call = _runtime.dispatch_before_provider_call
    _dispatch_after_provider_call = _runtime.dispatch_after_provider_call
    _dispatch_before_final_answer = _runtime.dispatch_before_final_answer
    _dispatch_stop = _runtime.dispatch_stop
    _budget = _runtime.budget
    effective_final_tool = _runtime.effective_final_tool
    _guard = _runtime.guard
    _guard_state = _runtime.guard_state
    _truncation_recovery = _runtime.truncation_recovery
    _truncation_attempts = _runtime.truncation_attempts
    _truncation_prefix = _runtime.truncation_prefix
    _pending_truncation_feedback = _runtime.pending_truncation_feedback
    _force_final_pending = _runtime.force_final_pending
    _align_defer_turn = _runtime.align_defer_turn
    checkpoint = _runtime.checkpoint
    ledger = _runtime.ledger
    active_primary = _runtime.active_primary

    def _push_runtime_state() -> None:
        _runtime.active_primary = active_primary
        _runtime.force_final_pending = _force_final_pending
        _runtime.truncation_attempts = _truncation_attempts
        _runtime.truncation_prefix = _truncation_prefix
        _runtime.pending_truncation_feedback = _pending_truncation_feedback

    def _pull_runtime_state() -> None:
        nonlocal active_primary, total, running_cost
        active_primary = _runtime.active_primary
        total = _runtime.total
        running_cost = _runtime.running_cost

    def _sync_ledger_totals() -> None:
        _push_runtime_state()
        _runtime.sync_ledger_totals()
        _pull_runtime_state()

    def _sync_checkpoint_accounting() -> None:
        _push_runtime_state()
        _runtime.sync_checkpoint_accounting()
        _pull_runtime_state()

    async def _save_checkpoint(
        phase: str,
        *,
        status: str = "running",
        turn_index: int | None = None,
        assistant_message: Message | None | object = None,
        assistant_stop_reason: str | None | object = None,
        pending_tool_blocks: list[ToolUseBlock] | None | object = None,
        completed_tool_results: dict[str, ToolResultBlock] | None | object = None,
    ) -> None:
        _push_runtime_state()
        await _runtime.save_checkpoint(
            phase,
            status=status,
            turn_index=turn_index,
            assistant_message=assistant_message,
            assistant_stop_reason=assistant_stop_reason,
            pending_tool_blocks=pending_tool_blocks,
            completed_tool_results=completed_tool_results,
        )
        _pull_runtime_state()

    _prepare_provider_call = _runtime.prepare_provider_call
    _load_pending_provider_request = _runtime.load_pending_provider_request
    checkpoint = _runtime.checkpoint

    async def _bind_active_primary(intent: ModelCallIntent) -> None:
        nonlocal active_primary
        _push_runtime_state()
        await _runtime.bind_active_primary(intent)
        active_primary = _runtime.active_primary

    from ..abort import any_signal, throw_if_aborted

    # Startup persistence shares the turn loop's signal and cleanup.
    max_turns = int(agent.max_turns) if isinstance(agent.max_turns, int) else 10**9
    signal = (
        any_signal(session._abort_controller, opts.signal)
        if opts.signal is not None
        else session._abort_controller
    )
    _runtime.signal = signal
    _final_result: _AgentResult | None = None

    # Native lifecycle bookkeeping is needed by the startup failure path too.
    _spans = _TurnLifecycle(session, run_id, _dispatch_lifecycle)
    _start_turn = _spans.start_turn
    _end_active_turn = _spans.end_active_turn
    _start_provider_call = _spans.start_provider_call
    _end_active_provider_call = _spans.end_active_provider_call
    _close_active_spans = _spans.close_active_spans

    _build_run_result = _runtime.build_run_result

    _startup_ctx = RunStartupContext(
        runtime=_runtime,
        prompt=prompt,
        opts=opts,
        checkpoint=checkpoint,
        resume_checkpoint=resume_checkpoint,
        save_checkpoint=_save_checkpoint,
        dispatch_user_prompt=_dispatch_user_prompt,
        dispatch_lifecycle=_dispatch_lifecycle,
    )
    try:
        async for _startup_event in initialize_run(_startup_ctx):
            yield _startup_event
        if _startup_ctx.blocked:
            return
        # Closed-loop schema-repair state (per-run; not checkpointed — a resumed
        # run starts with fresh retry counters).
        _max_schema_retries = int(getattr(agent, "structured_output_retries", 0) or 0)
        _gate_attempts = [0]  # [schema_attempts]
        # Re-entry guard: how many BeforeFinalAnswer-induced retries have been
        # honored this run. A hook that *always* asks to retry would otherwise bounce
        # the loop back every terminal until max_turns; the guard caps honored
        # retries at _MAX_FINAL_ANSWER_REENTRIES, then accepts the answer as-is.
        _final_answer_reentries = [0]

        # Terminal-answer finalization lives in loop/finalize.py; FinalizeCtx bundles
        # this run's dependencies so the call sites below stay short. The thin
        # wrappers copy the resolved _AgentResult back into _final_result, which the
        # turn loop reads to choose between returning and continuing.
        _fctx = FinalizeCtx(
            session=session,
            agent=agent,
            run_id=run_id,
            run_record=run_record,
            checkpoint=checkpoint,
            started=started,
            opts=opts,
            effective_final_tool=effective_final_tool,
            runtime=_runtime,
            max_schema_retries=_max_schema_retries,
            max_final_answer_reentries=_MAX_FINAL_ANSWER_REENTRIES,
            gate_attempts=_gate_attempts,
            final_answer_reentries=_final_answer_reentries,
            save_checkpoint=_save_checkpoint,
            end_active_turn=_end_active_turn,
            end_active_provider_call=_end_active_provider_call,
            dispatch_before_final_answer=_dispatch_before_final_answer,
            dispatch_stop=_dispatch_stop,
            usage_complete=lambda: ledger.usage_complete,
        )
        _finalization = RunFinalization(_fctx, _runtime)

        async def _emit_error_terminal(
            *,
            final_text_value: str | None = None,
            duration_ms: int | None = None,
            end_provider_call_usage: Usage | None = None,
        ) -> AsyncIterator[Event]:
            """Error exit shared by provider, hooks, and guard hard-stop paths."""
            nonlocal _final_result
            async for event in _finalization.emit_error(
                final_text_value=final_text_value,
                duration_ms=duration_ms,
                end_provider_call_usage=end_provider_call_usage,
            ):
                yield event
            _final_result = _finalization.result

        async def _finalize_final_tool_answer(
            turn_index: int,
            tool_blocks: list[ToolUseBlock],
            final_block: ToolUseBlock,
        ) -> AsyncIterator[Event]:
            nonlocal _final_result, active_primary
            active_primary = None
            _runtime.active_primary = None
            async for event in _finalization.finalize_final_tool(
                turn_index=turn_index,
                tool_blocks=tool_blocks,
                final_block=final_block,
                forced_final_turn=_forced_final_turn,
            ):
                yield event
            _final_result = _finalization.result

        async def _finalize_text_answer(
            turn_index: int,
            assembly: AssistantAssembly,
        ) -> AsyncIterator[Event]:
            nonlocal _final_result, active_primary
            active_primary = None
            _runtime.active_primary = None
            async for event in _finalization.finalize_text(
                turn_index=turn_index,
                assembly=assembly,
                forced_final_turn=_forced_final_turn,
            ):
                yield event
            _final_result = _finalization.result

        def _assembly_with_truncation_prefix(assembly: AssistantAssembly) -> AssistantAssembly:
            return assembly_with_truncation_prefix(assembly, _truncation_prefix)

        def _last_user_message_is_feedback(feedback: str) -> bool:
            return last_user_message_is_feedback(session, feedback)

        _drain_auxiliary_usage = _runtime.drain_auxiliary_usage
        _emit_primary_usage = _runtime.emit_primary_usage

        async def _emit_budget_exhausted() -> AsyncIterator[Event]:
            nonlocal _final_result
            async for event in _runtime.emit_budget_exhausted():
                yield event
            _final_result = _runtime.terminal_result

        start_turn = checkpoint.turn_index
        if resume_checkpoint is not None and checkpoint.phase == "turn_complete":
            start_turn = checkpoint.turn_index + 1

        _resume_state = ResumeState(
            start_turn=(
                checkpoint.turn_index + 1
                if resume_checkpoint is not None and checkpoint.phase == "turn_complete"
                else checkpoint.turn_index
            )
        )
        _resume_ctx = ResumeContext(
            session=session,
            agent=agent,
            run_id=run_id,
            run_record=run_record,
            checkpoint=checkpoint,
            resume_checkpoint=resume_checkpoint,
            ledger=ledger,
            signal=signal,
            exact_model_input=_exact_model_input,
            active_primary=active_primary,
            load_pending_provider_request=_load_pending_provider_request,
            maybe_save_provider_snapshot=_maybe_save_provider_snapshot,
            persist_event=lambda event: _persist_event(session, run_id, event),
            emit_error_terminal=_emit_error_terminal,
            drain_auxiliary_usage=_drain_auxiliary_usage,
            save_checkpoint=_save_checkpoint,
            state=_resume_state,
        )
        async for _resume_event in recover_resume(_resume_ctx):
            yield _resume_event
        if _resume_state.terminal:
            return
        _recovered_primary_projection = _resume_state.recovered_primary_projection
        _recovered_primary_prefix = _resume_state.recovered_primary_prefix
        _recovered_compaction = _resume_state.recovered_compaction
        start_turn = _resume_state.start_turn

        for turn_index in range(start_turn, max_turns):
            throw_if_aborted(signal)
            _replaying_frozen_attempt = (
                _exact_model_input
                and resume_checkpoint is not None
                and turn_index == checkpoint.turn_index
                and (
                    checkpoint.phase == "provider_pending"
                    or _recovered_primary_projection is not None
                )
            )
            # ── Budget pre-call check ─────────────────────────────────────
            # Before any turn span opens: an exhausted budget stops the run
            # gracefully (history intact, session reusable), mirroring the
            # max-turns tail below.
            if (
                _budget is not None
                and _budget.exceeded
                and not (_recovered_primary_projection is not None and turn_index == start_turn)
            ):
                async for event in _emit_budget_exhausted():
                    yield event
                return
            # A provider_pending resume continues an already-frozen turn. New
            # deliveries belong to the following turn; inserting them here
            # would place a user message between that request and its assistant.
            if not _replaying_frozen_attempt:
                async for note_event in _drain_pending_notifications(session, run_id):
                    yield note_event
                async for mail_event in _drain_mailbox(session, run_id):
                    yield mail_event
            # Bubble accumulated subagent events (incl. child PermissionRequests).
            async for child_event in _drain_child_events(session, run_id):
                yield child_event
            if session.interrupt_requested:
                session._reject_pending_alignment(
                    ConfigError("run interrupted before alignment was applied")
                )
                _dur = int((time.time() - started) * 1000)
                _final_result = _build_run_result("interrupted", "interrupted", duration_ms=_dur)
                _sync_checkpoint_accounting()
                event = ResultEvent(
                    subtype="interrupted",
                    stop_reason="interrupted",
                    total_usage=total,
                    duration_ms=_dur,
                    total_cost_usd=running_cost,
                    usage_complete=ledger.usage_complete,
                )
                await _persist_event(session, run_id, event)
                store = agent.run_store
                if run_record is not None and store is not None:
                    checkpoint.total_usage = total
                    await _complete_with_pending_events(session, run_id, checkpoint)
                yield event
                return
            if turn_index != _align_defer_turn:
                # Skipped only on the mid-turn-resumed turn itself: draining
                # there would put a user message between assistant(tool_use)
                # and its tool results. Entries drain at the next turn instead.
                async for align_event in _drain_alignment(session, run_id, _dispatch_user_prompt):
                    yield align_event
            await _start_turn(turn_index)
            if _pending_truncation_feedback is not None:
                feedback = _pending_truncation_feedback
                if not _last_user_message_is_feedback(feedback):
                    async for event in _gate_retry_tail(session, run_id=run_id, feedback=feedback):
                        yield event
                _pending_truncation_feedback = None
                await _end_active_turn()
                await _save_checkpoint("turn_complete", turn_index=turn_index)
                continue

            # Captured before _force_final_pending is reset below: a guard-
            # forced final answer must bypass the verification gates.
            _forced_final_turn = _force_final_pending
            session.compaction_retry_used_this_turn = False
            _primary_logical_call_id = str(uuid4())
            if (
                isinstance(active_primary, dict)
                and active_primary.get("turn_index") == turn_index
                and isinstance(active_primary.get("logical_call_id"), str)
            ):
                # Keep one stable parent operation across a frozen replay while
                # every physical delivery receives a fresh identity.
                _primary_logical_call_id = active_primary["logical_call_id"]
            _auxiliary_caller = _AccountedAuxiliaryCaller(
                agent.provider,
                ledger,
                session=session,
                limiter=getattr(agent, "limiter", None),
                parent_logical_call_id=_primary_logical_call_id,
                turn_index=turn_index,
            )
            _proactive_compaction_logical_call_id = str(uuid4())
            _forced_compaction_logical_call_id = str(uuid4())

            _prep_ctx = ProviderPreparationContext(
                session=session,
                agent=agent,
                opts=opts,
                turn_index=turn_index,
                run_id=run_id,
                signal=signal,
                checkpoint=checkpoint,
                resume_checkpoint=resume_checkpoint,
                start_turn=start_turn,
                exact_model_input=_exact_model_input,
                force_final_pending=_force_final_pending,
                forced_final_turn=_forced_final_turn,
                recovered_compaction=_recovered_compaction,
                recovered_primary_projection=_recovered_primary_projection,
                hook_dispatcher_active=hook_dispatcher.active,
                ledger=ledger,
                budget=_budget,
                auxiliary_caller=_auxiliary_caller,
                primary_logical_call_id=_primary_logical_call_id,
                proactive_compaction_logical_call_id=_proactive_compaction_logical_call_id,
                forced_compaction_logical_call_id=_forced_compaction_logical_call_id,
                load_pending_provider_request=_load_pending_provider_request,
                dispatch_before_provider_call=_dispatch_before_provider_call,
                persist_event=lambda event: _persist_event(session, run_id, event),
                drain_auxiliary_usage=_drain_auxiliary_usage,
                save_checkpoint=_save_checkpoint,
                emit_budget_exhausted=_emit_budget_exhausted,
                maybe_save_provider_snapshot=_maybe_save_provider_snapshot,
            )
            async for _prep_event in prepare_provider_turn(_prep_ctx):
                yield _prep_event
            if _prep_ctx.terminal:
                return
            req = _prep_ctx.outcome_req
            context_result = _prep_ctx.outcome_context
            resumed_assistant = _prep_ctx.resumed_assistant
            frozen_pending_resume = _prep_ctx.frozen_pending_resume
            exact_pending_replay = _prep_ctx.exact_pending_replay

            # Runs even when req is None (resumed-assistant turn) so stop-style
            # BeforeProviderCall hooks (e.g. StopPredicateHook) still fire on
            # resume; request mutation is simply ignored when there is no req.
            if (
                hook_dispatcher.active
                and not frozen_pending_resume
                and not (_recovered_primary_projection is not None and turn_index == start_turn)
                and not (resumed_assistant and checkpoint.phase == "tool_results_appended")
            ):
                (
                    req,
                    hook_events,
                    provider_action,
                    provider_feedback,
                ) = await _dispatch_before_provider_call(req, turn_index, context_result)
                for hook_event in hook_events:
                    await _persist_event(session, run_id, hook_event)
                    yield hook_event
                if provider_action == "continue":
                    _pending_tools = (
                        [
                            block
                            for block in checkpoint.assistant_message.content
                            if isinstance(block, ToolUseBlock)
                        ]
                        if resumed_assistant and checkpoint.assistant_message is not None
                        else []
                    )
                    if _pending_tools:
                        async for event in _final_tool_retry_tail(
                            session,
                            run_id=run_id,
                            tool_blocks=_pending_tools,
                            final_id=_pending_tools[0].id,
                            feedback=provider_feedback or "Provider hook requested another turn.",
                        ):
                            yield event
                    else:
                        async for event in _gate_retry_tail(
                            session,
                            run_id=run_id,
                            feedback=provider_feedback or "",
                        ):
                            yield event
                    await _end_active_turn()
                    await _save_checkpoint("turn_complete", turn_index=turn_index)
                    continue
                if provider_action == "stop_success":
                    if resumed_assistant and checkpoint.assistant_message is not None:
                        _stopped_tools = [
                            block
                            for block in checkpoint.assistant_message.content
                            if isinstance(block, ToolUseBlock)
                        ]
                        if _stopped_tools:
                            async for event in _final_tool_retry_tail(
                                session,
                                run_id=run_id,
                                tool_blocks=_stopped_tools,
                                final_id=_stopped_tools[0].id,
                                feedback="Provider hook stopped before tool execution.",
                                final_is_error=False,
                            ):
                                yield event
                    _dur = int((time.time() - started) * 1000)
                    _final_result = _build_run_result("success", "end_turn", duration_ms=_dur)
                    _sync_checkpoint_accounting()
                    await _end_active_turn()
                    async for event in _stop_when_tail(
                        session,
                        agent,
                        run_id=run_id,
                        run_record=run_record,
                        checkpoint=checkpoint,
                        total=total,
                        duration_ms=_dur,
                        running_cost=running_cost,
                        usage_complete=ledger.usage_complete,
                    ):
                        yield event
                    return
                if provider_action == "stop":
                    if resumed_assistant and checkpoint.assistant_message is not None:
                        _stopped_tools = [
                            block
                            for block in checkpoint.assistant_message.content
                            if isinstance(block, ToolUseBlock)
                        ]
                        if _stopped_tools:
                            async for event in _final_tool_retry_tail(
                                session,
                                run_id=run_id,
                                tool_blocks=_stopped_tools,
                                final_id=_stopped_tools[0].id,
                                feedback=provider_feedback or "Provider hook stopped the run.",
                            ):
                                yield event
                    async for event in _emit_error_terminal(final_text_value=provider_feedback):
                        yield event
                    return
            _provider_ctx = ProviderTurnContext(
                session=session,
                agent=agent,
                opts=opts,
                turn_index=turn_index,
                run_id=run_id,
                signal=signal,
                req=req,
                checkpoint=checkpoint,
                ledger=ledger,
                auxiliary_caller=_auxiliary_caller,
                exact_pending_replay=exact_pending_replay,
                resumed_assistant=resumed_assistant,
                forced_compactions_used=_forced_compactions_used,
                primary_logical_call_id=_primary_logical_call_id,
                forced_compaction_logical_call_id=_forced_compaction_logical_call_id,
                prepare_provider_call=_prepare_provider_call,
                start_provider_call=_start_provider_call,
                end_provider_call=_end_active_provider_call,
                bind_active_primary=_bind_active_primary,
                save_checkpoint=_save_checkpoint,
                drain_auxiliary_usage=_drain_auxiliary_usage,
                emit_primary_usage=_emit_primary_usage,
                emit_error_terminal=_emit_error_terminal,
                emit_budget_exhausted=_emit_budget_exhausted,
                dispatch_after_provider_call=_dispatch_after_provider_call,
                persist_event=lambda event: _persist_event(session, run_id, event),
                flush_events=lambda: _flush_events(session),
                maybe_save_provider_snapshot=_maybe_save_provider_snapshot,
                close_active_spans=_close_active_spans,
                sync_ledger_totals=_sync_ledger_totals,
                active_primary=active_primary,
                budget=_budget,
                force_final_pending=_force_final_pending,
            )

            async def _bind_provider_primary(
                intent: ModelCallIntent,
                _provider_ctx: ProviderTurnContext = _provider_ctx,
            ) -> None:
                await _bind_active_primary(intent)
                _provider_ctx.active_primary = _runtime.active_primary

            _provider_ctx.bind_active_primary = _bind_provider_primary

            async def _save_provider_checkpoint(
                phase: str,
                *,
                _provider_ctx: ProviderTurnContext = _provider_ctx,
                **kwargs: Any,
            ) -> None:
                nonlocal _force_final_pending
                # The provider phase may change this flag immediately before
                # its assistant checkpoint.  Adopt that phase-local value
                # before the shared runtime serializes the checkpoint.
                _force_final_pending = _provider_ctx.force_final_pending
                await _save_checkpoint(phase, **kwargs)

            _provider_ctx.save_checkpoint = _save_provider_checkpoint
            async for _provider_event in run_provider_turn(_provider_ctx):
                yield _provider_event
            assembly = _provider_ctx.assembly
            projection = _provider_ctx.projection
            control = _provider_ctx.control
            active_primary = _provider_ctx.active_primary
            _force_final_pending = _provider_ctx.force_final_pending
            if _provider_ctx.terminal:
                return
            if assembly is None:
                raise RuntimeError("provider stream ended without assistant assembly")

            # Resume the post-hook decision itself: hooks are never rerun to
            # guess an already accepted assistant's control flow.
            _control_projection = (
                projection if not resumed_assistant else _recovered_primary_projection
            )
            if (
                _control_projection is not None
                and _control_projection["after_action"] == "continue"
            ):
                if not resumed_assistant or _recovered_primary_prefix < 2:
                    control = _primary_control_message(
                        assembly.message, _control_projection["after_feedback"]
                    )
                    if _message_digest(control) != _control_projection["control_message_sha256"]:
                        raise ConfigError(
                            "primary control message differs from its acceptance projection"
                        )
                    await store_call(session, "session.append", session.append([control]))
                    event = UserEvent(message=control)
                    await _persist_event(session, run_id, event)
                    yield event
                await _end_active_turn()
                await _save_checkpoint("turn_complete", turn_index=turn_index)
                _recovered_primary_projection = None
                continue

            # ── Check for final_tool (terminal tool-use) ──────────────────
            if effective_final_tool and assembly.stop_reason == "tool_use":
                tool_blocks = [b for b in assembly.message.content if isinstance(b, ToolUseBlock)]
                final_block = next((b for b in tool_blocks if b.name == effective_final_tool), None)
                if final_block is not None:
                    async for event in _finalize_final_tool_answer(
                        turn_index, tool_blocks, final_block
                    ):
                        yield event
                    if _final_result is not None:
                        return
                    continue

            # ── Normal text response (stop_reason != tool_use) ───────────
            if assembly.stop_reason != "tool_use":
                # Opt-in truncation recovery: a text answer cut off by the
                # output-token limit is not finalized while attempts remain —
                # inject a continuation nudge and run again so the model can
                # finish. Skipped on a loop-guard forced-final turn. Off by
                # default (byte-identical).
                if (
                    _truncation_recovery is not None
                    and assembly.stop_reason == "max_tokens"
                    and not _forced_final_turn
                    and _truncation_attempts < _truncation_recovery.max_attempts
                ):
                    text = final_text(assembly.message)
                    if text is not None:
                        _truncation_prefix = f"{_truncation_prefix}{text}"
                    _truncation_attempts += 1
                    _pending_truncation_feedback = _truncation_recovery.feedback
                    await _save_checkpoint(
                        "assistant_appended",
                        turn_index=turn_index,
                        assistant_message=assembly.message,
                        assistant_stop_reason=assembly.stop_reason,
                    )
                    async for event in _gate_retry_tail(
                        session, run_id=run_id, feedback=_truncation_recovery.feedback
                    ):
                        yield event
                    _pending_truncation_feedback = None
                    await _end_active_turn()
                    await _save_checkpoint("turn_complete", turn_index=turn_index)
                    continue
                async for event in _finalize_text_answer(
                    turn_index, _assembly_with_truncation_prefix(assembly)
                ):
                    yield event
                if _final_result is not None:
                    return
                _truncation_prefix = ""
                _pending_truncation_feedback = None
                continue

            _tool_ctx = ToolBatchContext(
                session=session,
                agent=agent,
                signal=signal,
                run_id=run_id,
                turn_index=turn_index,
                assembly=assembly,
                checkpoint=checkpoint,
                resume_checkpoint=resume_checkpoint,
                stored_events=_runtime.stored_events,
                guard=_guard,
                guard_state=_guard_state,
                save_checkpoint=_save_checkpoint,
                flush_events=lambda: _flush_events(session),
                persist_event=lambda event: _persist_event(session, run_id, event),
                dispatch_lifecycle=_dispatch_lifecycle,
                emit_error_terminal=_emit_error_terminal,
                force_final_pending=_force_final_pending,
            )

            async def _save_tool_checkpoint(
                phase: str,
                *,
                _tool_ctx: ToolBatchContext = _tool_ctx,
                **kwargs: Any,
            ) -> None:
                nonlocal _force_final_pending
                # The loop-guard reminder sets this in the tool phase before
                # the turn-complete checkpoint is written.
                _force_final_pending = _tool_ctx.force_final_pending
                await _save_checkpoint(phase, **kwargs)

            _tool_ctx.save_checkpoint = _save_tool_checkpoint

            async for _tool_event in run_tool_batch(_tool_ctx):
                yield _tool_event
            _force_final_pending = _tool_ctx.force_final_pending
            if _tool_ctx.terminal:
                return
            # Natural end of turn body (guard said "continue" or "force_final").
            await _end_active_turn()
            await _save_checkpoint("turn_complete", turn_index=turn_index)

        # ── Max-turns exhausted ───────────────────────────────────────────
        _dur = int((time.time() - started) * 1000)
        _final_result = _build_run_result("error", "error", duration_ms=_dur)
        _sync_checkpoint_accounting()
        async for event in _max_turns_tail(
            session,
            run_id=run_id,
            runtime=_runtime,
            max_turns=max_turns,
            total=total,
            duration_ms=_dur,
            running_cost=running_cost,
            usage_complete=ledger.usage_complete,
        ):
            yield event
    except AbortError:
        async for event in _runtime.emit_abort_terminal(
            close_active_spans=_close_active_spans,
        ):
            yield event
    except LeaseLostError as exc:
        _runtime.lease_lost = exc
        async for event in _runtime.emit_exception_terminal(
            exc, close_active_spans=_close_active_spans
        ):
            yield event
    except Exception as exc:
        async for event in _runtime.emit_exception_terminal(
            exc, close_active_spans=_close_active_spans
        ):
            yield event
    finally:
        try:
            if _runtime.lease_lost is None:
                if (
                    _exact_model_input
                    and checkpoint.model_input_snapshot_id is not None
                    and checkpoint.phase != "provider_pending"
                ):
                    await _runtime.best_effort_snapshot_cleanup(
                        snapshot_id=checkpoint.model_input_snapshot_id
                    )
                    checkpoint.model_input_snapshot_id = None
                if checkpoint.phase not in {"started", "tool_results_appended"}:
                    await _flush_events(session)
        except LeaseLostError as exc:
            _runtime.lease_lost = exc
            async for _ in _runtime.emit_exception_terminal(exc):
                pass
        except Exception:
            pass
        session._event_journal = None
        if signal is not session._abort_controller:
            signal.close()
        await _close_active_spans(stop_reason="error")
        _run_result = (
            _runtime.terminal_result
            or _final_result
            or _build_run_result("error", "error", duration_ms=int((time.time() - started) * 1000))
        )
        if not _startup_ctx.blocked:
            await _dispatch_lifecycle(
                HookEvent.AGENT_STOP,
                AgentStopContext(
                    session=session,
                    run_id=run_id,
                    turn_index=_spans.active_turn_index,
                    deps=getattr(session, "run_deps", None),
                    result=_run_result,
                ),
            )
