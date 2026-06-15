"""The agent run loop: ``run_loop`` / ``resume_loop`` entry points and the
turn-by-turn ``_run_loop_impl`` generator that drives provider calls, tool
execution, guards, gates, budgets, and durable checkpoints."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from functools import partial
from typing import Any, cast
from uuid import uuid4

from ..compaction import (
    build_compaction_event,
    maybe_compact,
    reset_read_tracker_after_compaction,
)
from ..context import context_budget_to_dict
from ..errors import AbortError, ConfigError
from ..events import (
    AssistantEvent,
    BudgetEvent,
    ContextBuildEvent,
    ErrorEvent,
    Event,
    LoopGuardEvent,
    PermissionRequestEvent,
    ResultEvent,
    SkillsLoadedEvent,
    SystemEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    UsageEvent,
    UserEvent,
)
from ..hooks import (
    AgentStartContext,
    AgentStopContext,
    HookDispatcher,
    HookEvent,
    ProviderCallStartContext,
    ProviderCallStopContext,
    ToolUseStartContext,
    ToolUseStopContext,
    TurnStartContext,
    TurnStopContext,
)
from ..pricing import cost_usd as _cost_usd
from ..run_store import RunCheckpoint, RunRecord
from ..scheduler import execute_tool_calls
from ..session import RunOptions, Session
from ..types import (
    AssistantAssembly,
    ContentBlock,
    Message,
    StopReason,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from .checkpoint import (
    _background_workers_to_dict,
    _interrupted_tool_result_block,
    _last_message_has_tool_results,
    _last_message_matches,
    _loop_guard_state_from_dict,
    _loop_guard_state_to_dict,
    _persist_event,
    _queue_crashed_worker_notifications,
    _recover_completed_tool_results,
    _skill_overlay_from_dict,
    _skill_overlay_to_dict,
    _tool_result_block_from_end,
)
from .dispatch import (
    dispatch_after_provider_call,
    dispatch_before_final_answer,
    dispatch_before_provider_call,
    dispatch_lifecycle,
    dispatch_stop,
    dispatch_user_prompt,
)
from .finalize import (
    FinalizeCtx,
    TerminalOutcome,
    build_run_result,
    emit_error_terminal,
    finalize_final_tool_answer,
    finalize_text_answer,
)
from .request import (
    _build_context_result,
    _build_turn_request,
    _context_selected_tool_names,
    _re_inject_skill_context,
    build_user_message,
)
from .streaming import _stream_turn_with_compaction_retry, _stream_turn_with_ladder
from .terminals import (
    _budget_exhausted_tail,
    _error_result_tail,
    _gate_retry_tail,
    _max_turns_tail,
    _stop_when_tail,
)

# Max BeforeFinalAnswer-induced retries honored per run before the answer is
# accepted as-is. Keeps a perpetually-blocking final-answer hook from looping.
_MAX_FINAL_ANSWER_REENTRIES = 1


async def _drain_pending_notifications(
    session: Session,
    run_id: str,
) -> AsyncIterator[Event]:
    """Inject pending background-worker notifications into provider_view and yield UserEvents."""
    notifications = getattr(session, "pending_notifications", None)
    if not notifications:
        return
    to_drain = list(notifications)
    notifications.clear()
    for note in to_drain:
        await session.append([note])
        event: Event = UserEvent(message=note, subtype="notification")
        await _persist_event(session, run_id, event)
        yield event


def _render_peer_message(message: Any) -> Message:
    """Wrap a drained :class:`MailboxMessage` as a ``<peer-message>`` user message."""
    from xml.sax.saxutils import escape

    parts = [
        "<peer-message>",
        f"<from>{escape(message.sender)}</from>",
        f"<type>{escape(message.type)}</type>",
    ]
    if message.request_id:
        parts.append(f"<request-id>{escape(message.request_id)}</request-id>")
    if message.in_reply_to:
        parts.append(f"<in-reply-to>{escape(message.in_reply_to)}</in-reply-to>")
    parts.append(f"<content>{escape(message.content)}</content>")
    parts.append("</peer-message>")
    return Message(role="user", content=[TextBlock(text="".join(parts))])


async def _drain_mailbox(session: Session, run_id: str) -> AsyncIterator[Event]:
    """Drain peer messages for this session's address into provider_view.

    No-op (byte-identical) unless the agent has a ``mailbox`` and the session has
    a ``mailbox_address``. Mirrors :func:`_drain_pending_notifications`.
    """
    mailbox = getattr(session.agent, "mailbox", None)
    address = getattr(session, "mailbox_address", None)
    if mailbox is None or not address:
        return
    messages = await mailbox.drain(address)
    for message in messages:
        note = _render_peer_message(message)
        await session.append([note])
        event: Event = UserEvent(message=note, subtype="notification")
        await _persist_event(session, run_id, event)
        yield event


async def _drain_alignment(
    session: Session,
    run_id: str,
    dispatch_user_prompt_fn: Callable[
        [str, list[dict[str, str]] | None, str],
        Awaitable[tuple[str, list[dict[str, str]] | None, list[Event], str | None]],
    ],
) -> AsyncIterator[Event]:
    queue = getattr(session, "alignment_queue", None)
    if not queue:
        return
    entries = list(queue)
    queue.clear()
    for entry in entries:
        try:
            prompt, images, hook_events, block_reason = await dispatch_user_prompt_fn(
                entry.prompt,
                entry.images,
                "align",
            )
            for hook_event in hook_events:
                await _persist_event(session, run_id, hook_event)
                yield hook_event
            if block_reason is not None:
                # Hook blocked this align: drop only this message (reject its
                # promise), no ErrorEvent — remaining queued messages still
                # inject. The block was already surfaced via hook_events above.
                if not entry.future.done():
                    entry.future.set_exception(ConfigError(block_reason))
                continue
            user_message = build_user_message(prompt, images)
            await session.append([user_message])
            event: Event = UserEvent(message=user_message, subtype="alignment")
            await _persist_event(session, run_id, event)
            if not entry.future.done():
                entry.future.set_result(None)
            yield event
        except Exception as exc:
            # Unexpected fault applying this align (dispatch/build/append/persist).
            # Notify the align() caller AND surface it on the event stream — even
            # when the future is already done — so it is never silently dropped.
            if not entry.future.done():
                entry.future.set_exception(exc)
            error_event: Event = ErrorEvent(
                error={"name": type(exc).__name__, "message": str(exc), "retryable": False}
            )
            await _persist_event(session, run_id, error_event)
            yield error_event


async def _drain_child_events(session: Session, run_id: str) -> AsyncIterator[Event]:
    """Surface accumulated subagent events to the parent's event stream.

    Foreground/background subagents append their events (wrapped as
    ``SubagentEvent``) to ``session.pending_child_events``. Yielding them here —
    at the same top-of-turn chokepoint as the other drains — bubbles a child's
    ``PermissionRequestEvent`` (and every other child event) up to the parent
    caller's iterator instead of leaving it in a buffer only host UIs can poll.
    These are observational and are *not* injected into ``provider_view`` (the
    subagent's tool result already represents the work). No-op (byte-identical)
    when no subagent has run.
    """
    pending = getattr(session, "pending_child_events", None)
    if not pending:
        return
    to_drain = list(pending)
    pending.clear()
    for event in to_drain:
        await _persist_event(session, run_id, event)
        yield event


async def _cancel_background_workers(session: Session) -> None:
    """Cancel any running asyncio.Tasks in session.workers (abort cleanup)."""
    import asyncio

    workers = getattr(session, "workers", None)
    for handle in (workers or {}).values():
        task = getattr(handle, "task", None)
        if task is not None and isinstance(task, asyncio.Task) and not task.done():
            task.cancel()
    for task in getattr(session, "background_tasks", None) or []:
        if isinstance(task, asyncio.Task) and not task.done():
            task.cancel()


async def run_loop(session: Session, prompt: str, opts: RunOptions) -> AsyncIterator[Event]:
    store = session.agent.run_store
    run_record = await store.create_run(session.id) if store is not None else None
    run_id = run_record.id if run_record is not None else str(uuid4())
    async for event in _run_loop_impl(
        session,
        prompt,
        opts,
        run_id=run_id,
        run_record=run_record,
        resume_checkpoint=None,
    ):
        yield event


async def resume_loop(session: Session, run_id: str, opts: RunOptions) -> AsyncIterator[Event]:
    store = session.agent.run_store
    if store is None:
        raise RuntimeError("Agent has no run_store configured")
    run_record = await store.load_run(run_id)
    if run_record is None:
        raise KeyError(f"run not found: {run_id}")
    if run_record.session_id != session.id:
        raise ValueError(f"run {run_id} belongs to session {run_record.session_id}")
    if run_record.status in {"completed", "failed", "aborted"}:
        return
    checkpoint = run_record.checkpoint
    if checkpoint is None:
        return
    async for event in _run_loop_impl(
        session,
        checkpoint.prompt,
        opts,
        run_id=run_record.id,
        run_record=run_record,
        resume_checkpoint=checkpoint,
    ):
        yield event


class _SpanLifecycle:
    """Tracks the open turn/provider-call observer spans for one run.

    Owns the two pieces of mutable span state (`active_turn_index`,
    `active_provider_call`) that the turn loop previously threaded through
    `nonlocal` closures, and dispatches the matching lifecycle hook events
    via the run's `_dispatch_lifecycle` callback.
    """

    def __init__(
        self,
        session: Session,
        run_id: str,
        dispatch: Callable[[HookEvent, Any], Awaitable[None]],
    ) -> None:
        self._session = session
        self._run_id = run_id
        self._dispatch = dispatch
        self.active_turn_index: int | None = None
        self.active_provider_call: tuple[int, str, float] | None = None

    async def start_turn(self, turn_index: int) -> None:
        await self._dispatch(
            HookEvent.TURN_START,
            TurnStartContext(
                session=self._session,
                run_id=self._run_id,
                turn_index=turn_index,
                deps=getattr(self._session, "run_deps", None),
            ),
        )
        self.active_turn_index = turn_index

    async def end_active_turn(self) -> None:
        if self.active_turn_index is None:
            return
        turn_index = self.active_turn_index
        self.active_turn_index = None
        await self._dispatch(
            HookEvent.TURN_STOP,
            TurnStopContext(
                session=self._session,
                run_id=self._run_id,
                turn_index=turn_index,
                deps=getattr(self._session, "run_deps", None),
            ),
        )

    async def start_provider_call(self, turn_index: int, model: str) -> None:
        started_at = time.perf_counter()
        await self._dispatch(
            HookEvent.PROVIDER_CALL_START,
            ProviderCallStartContext(
                session=self._session,
                run_id=self._run_id,
                turn_index=turn_index,
                deps=getattr(self._session, "run_deps", None),
                model=model,
            ),
        )
        self.active_provider_call = (turn_index, model, started_at)

    async def end_active_provider_call(
        self, *, stop_reason: str, usage: Usage | None = None
    ) -> None:
        if self.active_provider_call is None:
            return
        turn_index, model, started_at = self.active_provider_call
        self.active_provider_call = None
        await self._dispatch(
            HookEvent.PROVIDER_CALL_STOP,
            ProviderCallStopContext(
                session=self._session,
                run_id=self._run_id,
                turn_index=turn_index,
                deps=getattr(self._session, "run_deps", None),
                model=model,
                stop_reason=stop_reason,
                usage=usage or Usage(),
                duration_ms=int((time.perf_counter() - started_at) * 1000),
            ),
        )

    async def close_active_observer_spans(self, *, stop_reason: str = "error") -> None:
        await self.end_active_provider_call(stop_reason=stop_reason)
        await self.end_active_turn()


async def _run_loop_impl(  # pyright: ignore[reportGeneralTypeIssues]
    session: Session,
    prompt: str,
    opts: RunOptions,
    *,
    run_id: str,
    run_record: RunRecord | None,
    resume_checkpoint: RunCheckpoint | None,
) -> AsyncIterator[Event]:
    agent = session.agent
    session.active_run_id = run_id
    # Reset run-level model-fallback state so each run starts on the primary model.
    session.active_model = None
    session.fallback_index = 0
    started = time.time()
    total = Usage()
    running_cost: float | None = None  # accumulated USD cost; None until first priced turn
    # Per-run circuit breaker for ladder-driven forced compactions (Agent(
    # compaction_ladder=...)); one-element cell shared with the recovery
    # generator.  Unused when no ladder is configured.
    _forced_compactions_used = [0]

    from ..observability import RunResultInfo as _RunResultInfo

    _hooks = list(getattr(agent, "hooks", None) or [])
    hook_dispatcher = HookDispatcher(_hooks)

    # Bind the hook-dispatch wrappers (in loop/dispatch.py) to this run's
    # dispatcher/session/run_id so the call sites below stay argument-light.
    _dispatch_user_prompt = partial(dispatch_user_prompt, hook_dispatcher, session, run_id)
    _dispatch_lifecycle = partial(dispatch_lifecycle, hook_dispatcher)
    _dispatch_before_provider_call = partial(
        dispatch_before_provider_call, hook_dispatcher, session, run_id
    )
    _dispatch_after_provider_call = partial(
        dispatch_after_provider_call, hook_dispatcher, session, run_id
    )
    _dispatch_before_final_answer = partial(
        dispatch_before_final_answer, hook_dispatcher, session, run_id
    )
    _dispatch_stop = partial(dispatch_stop, hook_dispatcher, session, run_id)

    # Resolve per-run deps: RunOptions.deps wins over Agent.deps
    session.run_deps = opts.deps if opts.deps is not None else getattr(agent, "deps", None)

    # Resolve the run budget: RunOptions.budget > inherited-from-parent
    # (subagent child sessions) > Agent.budget.  All runs in an agent tree
    # share one RunBudget object, so child spending is visible here.
    _budget = (
        opts.budget or getattr(session, "inherited_budget", None) or getattr(agent, "budget", None)
    )
    session.active_budget = _budget

    # Resolve final_tool_name: RunOptions wins over Agent
    effective_final_tool = opts.final_tool_name or getattr(agent, "final_tool_name", None)

    # Feature A — when the provider supports native structured output via the
    # forced-tool method (e.g. AnthropicProvider), wire the output schema name
    # as the terminal tool so the loop captures final_block.input as
    # structured_output without executing a real tool.  Explicit final_tool_name
    # wins if already set.
    if effective_final_tool is None:
        _schema = opts.output_schema or getattr(agent, "output_schema", None)
        if _schema is not None and hasattr(agent.provider, "capabilities"):
            _provider_caps = agent.provider.capabilities(agent.model)
            if getattr(_provider_caps, "structured_output", False):
                effective_final_tool = _schema.name

    # Loop guard — detects repeated identical tool calls and consecutive
    # failure streaks.  On by default (Agent sets self.loop_guard = LoopGuard()
    # unless the caller passes loop_guard=None).
    from ..loop_guard import LoopGuardState, evaluate_loop_guard

    _guard = getattr(agent, "loop_guard", None)
    _guard_state = LoopGuardState() if _guard is not None else None
    _force_final_pending = False
    if resume_checkpoint is not None:
        total = resume_checkpoint.total_usage
        # Restore running_cost from the restored usage so the resumed run's cost
        # covers the whole run, not just post-resume turns. cost_usd returns None
        # for unknown models, preserving "None until first priced turn" semantics.
        running_cost = _cost_usd(total, agent.model)
        if _guard is not None:
            _guard_state = _loop_guard_state_from_dict(resume_checkpoint.loop_guard_state)
            if _guard_state is None:
                _guard_state = LoopGuardState()
        _force_final_pending = resume_checkpoint.force_final_pending
        session.pending_skill_overlay = _skill_overlay_from_dict(
            resume_checkpoint.pending_skill_overlay
        )
        session.current_turn_allowed_tools = resume_checkpoint.current_turn_allowed_tools
        session.current_turn_permission_decisions = dict(resume_checkpoint.permission_decisions)

    checkpoint = resume_checkpoint or RunCheckpoint(
        phase="started",
        prompt=prompt,
        turn_index=0,
        total_usage=total,
    )

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
        store = agent.run_store
        if run_record is None or store is None:
            return
        nonlocal checkpoint
        checkpoint.phase = phase  # type: ignore[assignment]
        if turn_index is not None:
            checkpoint.turn_index = turn_index
        checkpoint.total_usage = total
        if assistant_message is not None:
            checkpoint.assistant_message = (
                assistant_message if isinstance(assistant_message, Message) else None
            )
        if assistant_stop_reason is not None:
            checkpoint.assistant_stop_reason = (
                assistant_stop_reason if isinstance(assistant_stop_reason, str) else None
            )
        if pending_tool_blocks is not None:
            checkpoint.pending_tool_blocks = (
                list(pending_tool_blocks) if isinstance(pending_tool_blocks, list) else []
            )
        if completed_tool_results is not None:
            checkpoint.completed_tool_results = (
                dict(completed_tool_results) if isinstance(completed_tool_results, dict) else {}
            )
        checkpoint.force_final_pending = _force_final_pending
        checkpoint.loop_guard_state = _loop_guard_state_to_dict(_guard_state)
        checkpoint.pending_skill_overlay = _skill_overlay_to_dict(session.pending_skill_overlay)
        checkpoint.current_turn_allowed_tools = session.current_turn_allowed_tools
        checkpoint.permission_decisions = dict(session.current_turn_permission_decisions)
        checkpoint.background_workers = _background_workers_to_dict(
            session, checkpoint.background_workers
        )
        await store.save_checkpoint(run_id, checkpoint, status=status)

    async def _handle_prompt_block(block_reason: str) -> AsyncIterator[Event]:
        # Terminal path for a UserPromptSubmit hook that blocked the prompt.
        # It returns before the main try/finally, so the AgentStop lifecycle is
        # dispatched here — otherwise observers/telemetry never see on_run_end.
        _dur = int((time.time() - started) * 1000)
        blocked_result = _RunResultInfo(
            run_id=run_id,
            session_id=session.id,
            subtype="error",
            stop_reason="error",
            total_usage=total,
            duration_ms=_dur,
        )
        event: Event = ErrorEvent(
            error={"name": "HookBlockedPrompt", "message": block_reason, "retryable": False}
        )
        await _persist_event(session, run_id, event)
        yield event
        async for event in _error_result_tail(
            session,
            agent,
            run_id=run_id,
            run_record=run_record,
            checkpoint=checkpoint,
            total=total,
            duration_ms=_dur,
            running_cost=running_cost,
        ):
            yield event
        await _dispatch_lifecycle(
            HookEvent.AGENT_STOP,
            AgentStopContext(
                session=session,
                run_id=run_id,
                turn_index=None,
                deps=getattr(session, "run_deps", None),
                result=blocked_result,
            ),
        )

    await _dispatch_lifecycle(
        HookEvent.AGENT_START,
        AgentStartContext(
            session=session,
            run_id=run_id,
            turn_index=None,
            deps=getattr(session, "run_deps", None),
            model=agent.model,
            prompt=prompt,
            tools=tuple(sorted(tool.name for tool in agent.tools.list())),
        ),
    )

    if resume_checkpoint is None:
        await _save_checkpoint("started")
        event = SystemEvent(
            session_id=session.id,
            run_id=run_id,
            model=agent.model,
            tools=sorted(tool.name for tool in agent.tools.list()),
            permission_mode=agent.permission_engine.mode,
            cwd=agent.cwd,
        )
        await _persist_event(session, run_id, event)
        yield event

        if not session.skills_loaded_emitted and agent.skills:
            session.skills_loaded_emitted = True
            skills_data = [
                {
                    "name": s.name,
                    "description": s.frontmatter.description,
                    **(
                        {"when_to_use": s.frontmatter.when_to_use}
                        if s.frontmatter.when_to_use
                        else {}
                    ),
                    **(
                        {"argument_hint": s.frontmatter.argument_hint}
                        if s.frontmatter.argument_hint
                        else {}
                    ),
                }
                for s in sorted(agent.skills.values(), key=lambda x: x.name)
            ]
            event = SkillsLoadedEvent(skills=skills_data)
            await _persist_event(session, run_id, event)
            yield event

        prompt, images, hook_events, block_reason = await _dispatch_user_prompt(
            prompt, opts.images, "run"
        )
        for hook_event in hook_events:
            await _persist_event(session, run_id, hook_event)
            yield hook_event
        if block_reason is not None:
            async for event in _handle_prompt_block(block_reason):
                yield event
            return
        user_message = build_user_message(prompt, images)
        if agent.skill_listing_text and not session.tools_override:
            from ..skills.system_reminder import wrap_in_system_reminder

            reminder = wrap_in_system_reminder(agent.skill_listing_text)
            user_message.content.insert(0, TextBlock(text=reminder))
        await session.append([user_message])
        await _save_checkpoint("user_appended")
        event = UserEvent(message=user_message, subtype="prompt")
        await _persist_event(session, run_id, event)
        yield event
    elif checkpoint.phase == "started":
        prompt, images, hook_events, block_reason = await _dispatch_user_prompt(
            prompt, opts.images, "run"
        )
        for hook_event in hook_events:
            await _persist_event(session, run_id, hook_event)
            yield hook_event
        if block_reason is not None:
            async for event in _handle_prompt_block(block_reason):
                yield event
            return
        user_message = build_user_message(prompt, images)
        if agent.skill_listing_text and not session.tools_override:
            from ..skills.system_reminder import wrap_in_system_reminder

            reminder = wrap_in_system_reminder(agent.skill_listing_text)
            user_message.content.insert(0, TextBlock(text=reminder))
        if not _last_message_matches(session, user_message):
            await session.append([user_message])
        await _save_checkpoint("user_appended")
        event = UserEvent(message=user_message, subtype="prompt")
        await _persist_event(session, run_id, event)
        yield event

    from ..abort import any_signal, throw_if_aborted

    max_turns = int(agent.max_turns) if isinstance(agent.max_turns, int) else 10**9
    signal = (
        any_signal(session._abort_controller, opts.signal)
        if opts.signal is not None
        else session._abort_controller
    )
    _final_result: _RunResultInfo | None = None
    # Closed-loop schema-repair state (per-run; not checkpointed — a resumed
    # run starts with fresh retry counters).
    _max_schema_retries = int(getattr(agent, "structured_output_retries", 0) or 0)
    _gate_attempts = [0]  # [schema_attempts]
    # Re-entry guard: how many BeforeFinalAnswer-induced retries have been
    # honored this run. A hook that *always* asks to retry would otherwise bounce
    # the loop back every terminal until max_turns; the guard caps honored
    # retries at _MAX_FINAL_ANSWER_REENTRIES, then accepts the answer as-is.
    _final_answer_reentries = [0]

    # Observer/hook span bookkeeping (turn + provider-call spans) lives in a
    # small helper so the turn loop no longer threads its mutable span state
    # through nonlocal closures. Local aliases keep the call sites unchanged.
    _spans = _SpanLifecycle(session, run_id, _dispatch_lifecycle)
    _start_turn = _spans.start_turn
    _end_active_turn = _spans.end_active_turn
    _start_provider_call = _spans.start_provider_call
    _end_active_provider_call = _spans.end_active_provider_call
    _close_active_observer_spans = _spans.close_active_observer_spans

    def _build_run_result(
        subtype: str,
        stop_reason: str,
        *,
        duration_ms: int,
        error: dict[str, Any] | None = None,
    ) -> _RunResultInfo:
        """Build the run's terminal :class:`RunResultInfo` from current run state.

        Used at the loop exits that don't go through the finalizers (stop_when,
        budget, max-turns, abort, exception, finally)."""
        return build_run_result(
            run_id=run_id,
            session_id=session.id,
            subtype=subtype,
            stop_reason=stop_reason,
            total=total,
            duration_ms=duration_ms,
            error=error,
        )

    # Terminal-answer finalization lives in loop/finalize.py; FinalizeCtx bundles
    # this run's dependencies so the call sites below stay short. The thin
    # wrappers copy the resolved RunResultInfo back into _final_result, which the
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
        max_schema_retries=_max_schema_retries,
        max_final_answer_reentries=_MAX_FINAL_ANSWER_REENTRIES,
        gate_attempts=_gate_attempts,
        final_answer_reentries=_final_answer_reentries,
        save_checkpoint=_save_checkpoint,
        end_active_turn=_end_active_turn,
        end_active_provider_call=_end_active_provider_call,
        dispatch_before_final_answer=_dispatch_before_final_answer,
        dispatch_stop=_dispatch_stop,
    )

    async def _emit_error_terminal(
        *,
        final_text_value: str | None = None,
        duration_ms: int | None = None,
        end_provider_call_usage: Usage | None = None,
    ) -> AsyncIterator[Event]:
        """Error exit shared by the provider/after/stop-hook and guard hard-stop
        paths. The caller still issues ``return`` after iterating this."""
        nonlocal _final_result
        outcome = TerminalOutcome()
        async for event in emit_error_terminal(
            _fctx,
            total=total,
            running_cost=running_cost,
            outcome=outcome,
            final_text_value=final_text_value,
            duration_ms=duration_ms,
            end_provider_call_usage=end_provider_call_usage,
        ):
            yield event
        _final_result = outcome.result

    async def _finalize_final_tool_answer(
        turn_index: int,
        tool_blocks: list[ToolUseBlock],
        final_block: ToolUseBlock,
    ) -> AsyncIterator[Event]:
        nonlocal _final_result
        outcome = TerminalOutcome()
        async for event in finalize_final_tool_answer(
            _fctx,
            turn_index=turn_index,
            tool_blocks=tool_blocks,
            final_block=final_block,
            total=total,
            running_cost=running_cost,
            forced_final_turn=_forced_final_turn,
            outcome=outcome,
        ):
            yield event
        _final_result = outcome.result

    async def _finalize_text_answer(
        turn_index: int,
        assembly: AssistantAssembly,
    ) -> AsyncIterator[Event]:
        nonlocal _final_result
        outcome = TerminalOutcome()
        async for event in finalize_text_answer(
            _fctx,
            turn_index=turn_index,
            assembly=assembly,
            total=total,
            running_cost=running_cost,
            forced_final_turn=_forced_final_turn,
            outcome=outcome,
        ):
            yield event
        _final_result = outcome.result

    start_turn = checkpoint.turn_index
    if resume_checkpoint is not None and checkpoint.phase in {
        "tool_results_appended",
        "turn_complete",
    }:
        start_turn = checkpoint.turn_index + 1

    try:
        # Crashed-worker notifications must yield from INSIDE the try so that a
        # caller closing the generator here (aclose -> GeneratorExit at the yield)
        # still runs the finally block that closes observer spans.
        if resume_checkpoint is not None:
            for worker_event in await _queue_crashed_worker_notifications(
                session, checkpoint, run_id
            ):
                yield worker_event
            if checkpoint.background_workers:
                await _save_checkpoint(checkpoint.phase, turn_index=checkpoint.turn_index)

        for turn_index in range(start_turn, max_turns):
            throw_if_aborted(signal)
            # ── Budget pre-call check ─────────────────────────────────────
            # Before any turn span opens: an exhausted budget stops the run
            # gracefully (history intact, session reusable), mirroring the
            # max-turns tail below.
            if _budget is not None and _budget.exceeded:
                _dur = int((time.time() - started) * 1000)
                _final_result = _build_run_result("error", "error", duration_ms=_dur)
                async for event in _budget_exhausted_tail(
                    session,
                    agent,
                    run_id=run_id,
                    run_record=run_record,
                    checkpoint=checkpoint,
                    budget=_budget,
                    total=total,
                    duration_ms=_dur,
                    running_cost=running_cost,
                ):
                    yield event
                return
            # Drain background-worker notifications before this turn's provider call.
            async for note_event in _drain_pending_notifications(session, run_id):
                yield note_event
            # Drain peer mailbox messages addressed to this session, same chokepoint.
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
                event = ResultEvent(
                    subtype="interrupted",
                    stop_reason="interrupted",
                    total_usage=total,
                    duration_ms=_dur,
                    total_cost_usd=running_cost,
                )
                await _persist_event(session, run_id, event)
                store = agent.run_store
                if run_record is not None and store is not None:
                    checkpoint.total_usage = total
                    await store.mark_completed(run_id, checkpoint)
                yield event
                return
            async for align_event in _drain_alignment(session, run_id, _dispatch_user_prompt):
                yield align_event
            await _start_turn(turn_index)
            # Captured before _force_final_pending is reset below: a guard-
            # forced final answer must bypass the verification gates.
            _forced_final_turn = _force_final_pending
            session.compaction_retry_used_this_turn = False

            pending = session.pending_skill_overlay
            session.pending_skill_overlay = None
            model_override = None
            if pending is not None:
                session.current_turn_allowed_tools = pending.allowed_tools
                model_override = pending.model_override
            else:
                session.current_turn_allowed_tools = None
            # Clear per-turn permission decisions on every fresh turn.
            # Preserve them only when resuming the exact checkpointed turn so
            # Seam A can replay stored allow/deny without re-prompting.
            if resume_checkpoint is None or turn_index != checkpoint.turn_index:
                session.current_turn_permission_decisions = {}

            resumed_assistant = (
                resume_checkpoint is not None
                and turn_index == checkpoint.turn_index
                and checkpoint.phase
                in {
                    "assistant_appended",
                    "permission_pending",
                    "tool_batch_pending",
                    "tool_executing",
                }
                and checkpoint.assistant_message is not None
            )
            if resumed_assistant:
                session.current_turn_allowed_tools = checkpoint.current_turn_allowed_tools
            if (
                resume_checkpoint is not None
                and not resumed_assistant
                and turn_index == checkpoint.turn_index
                and checkpoint.phase == "provider_pending"
                and session.provider_view
                and session.provider_view[-1].role == "assistant"
            ):
                last_assistant = session.provider_view[-1]
                checkpoint.assistant_message = last_assistant
                checkpoint.assistant_stop_reason = (
                    "tool_use"
                    if any(isinstance(block, ToolUseBlock) for block in last_assistant.content)
                    else "end_turn"
                )
                resumed_assistant = True

            if not resumed_assistant and await maybe_compact(session, agent, signal):
                reset_read_tracker_after_compaction(session, agent)
                event = build_compaction_event(session)
                await _persist_event(session, run_id, event)
                yield event
                _re_inject_skill_context(session)

            # ── Context building (RAG-per-turn, schema injection, …) ──────
            context_result = (
                None if resumed_assistant else await _build_context_result(session, turn_index)
            )
            if context_result is not None:
                event = ContextBuildEvent(
                    system_blocks=len(context_result.system_blocks),
                    messages=len(context_result.messages),
                    selected_tools=_context_selected_tool_names(context_result),
                    budget=context_budget_to_dict(context_result.budget),
                    metadata=dict(context_result.metadata),
                )
                await _persist_event(session, run_id, event)
                yield event

            req = (
                None
                if resumed_assistant
                else _build_turn_request(
                    session, opts, context=context_result, model_override=model_override
                )
            )

            # If the previous turn tripped the guard with force_final, strip
            # all tools so the model must produce a text response.
            if req is not None and _force_final_pending:
                req.tools = []
                req.tool_choice = None

            # Runs even when req is None (resumed-assistant turn) so stop-style
            # BeforeProviderCall hooks (e.g. StopPredicateHook) still fire on
            # resume; request mutation is simply ignored when there is no req.
            if hook_dispatcher.active:
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
                    _dur = int((time.time() - started) * 1000)
                    _final_result = _build_run_result("success", "end_turn", duration_ms=_dur)
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
                    ):
                        yield event
                    return
                if provider_action == "stop":
                    async for event in _emit_error_terminal(final_text_value=provider_feedback):
                        yield event
                    return

            assembly: AssistantAssembly | None = None
            if resumed_assistant:
                assert checkpoint.assistant_message is not None
                assembly = AssistantAssembly(
                    message=checkpoint.assistant_message,
                    stop_reason=cast(StopReason, checkpoint.assistant_stop_reason or "tool_use"),
                    usage=Usage(),
                )
            else:
                assert req is not None
                _ladder = getattr(agent, "compaction_ladder", None)
                if _ladder is None:
                    # Legacy path: one forced-compaction retry per turn.
                    _provider_stream = _stream_turn_with_compaction_retry(
                        session,
                        agent,
                        opts,
                        req,
                        turn_index=turn_index,
                        signal=signal,
                        save_checkpoint=_save_checkpoint,
                        start_provider_call=_start_provider_call,
                        end_provider_call=_end_active_provider_call,
                    )
                else:
                    # Ladder recovery: micro-compact, then capped forced compactions.
                    _provider_stream = _stream_turn_with_ladder(
                        session,
                        agent,
                        opts,
                        req,
                        turn_index=turn_index,
                        signal=signal,
                        ladder=_ladder,
                        forced_used=_forced_compactions_used,
                        save_checkpoint=_save_checkpoint,
                        start_provider_call=_start_provider_call,
                        end_provider_call=_end_active_provider_call,
                    )
                async for item in _provider_stream:
                    if isinstance(item, AssistantAssembly):
                        assembly = item
                    else:
                        await _persist_event(session, run_id, item)
                        yield item

            if assembly is None:
                raise RuntimeError("provider stream ended without assistant assembly")

            if not resumed_assistant:
                # Charge accounting from the provider's actual usage, captured
                # before any AfterProviderCall hook can mutate the assembly.
                _provider_usage = assembly.usage
                (
                    assembly,
                    hook_events,
                    after_action,
                    after_feedback,
                ) = await _dispatch_after_provider_call(
                    assembly,
                    turn_index,
                )
                for hook_event in hook_events:
                    await _persist_event(session, run_id, hook_event)
                    yield hook_event
                if after_action == "stop":
                    async for event in _emit_error_terminal(
                        final_text_value=after_feedback,
                        end_provider_call_usage=_provider_usage,
                    ):
                        yield event
                    return
                await _end_active_provider_call(
                    stop_reason=assembly.stop_reason,
                    usage=_provider_usage,
                )

                await session.append([assembly.message])
                total = total.add(_provider_usage)
                # req is guaranteed non-None here (not resumed_assistant branch);
                # fall back to agent.model to satisfy the type checker.
                _turn_model = req.model if req is not None else agent.model
                _turn_cost = _cost_usd(_provider_usage, _turn_model)
                if _turn_cost is not None:
                    running_cost = (running_cost or 0.0) + _turn_cost
                if _budget is not None:
                    _budget.charge(_provider_usage, _turn_cost)
                _force_final_pending = False
                await _save_checkpoint(
                    "assistant_appended",
                    turn_index=turn_index,
                    assistant_message=assembly.message,
                    assistant_stop_reason=assembly.stop_reason,
                )
                event = AssistantEvent(message=assembly.message, stop_reason=assembly.stop_reason)
                await _persist_event(session, run_id, event)
                yield event
                session.last_usage = _provider_usage
                event = UsageEvent(
                    usage=_provider_usage,
                    cumulative=total,
                    cost_usd=_turn_cost,
                    cumulative_cost_usd=running_cost,
                )
                await _persist_event(session, run_id, event)
                yield event
                if _budget is not None and _budget.take_warning():
                    event = BudgetEvent(
                        kind="warning",
                        spent_tokens=_budget.spent_tokens,
                        spent_usd=_budget.spent_usd,
                        max_tokens=_budget.max_tokens,
                        max_cost_usd=_budget.max_cost_usd,
                    )
                    await _persist_event(session, run_id, event)
                    yield event

                # AfterProviderCall hook requested another turn: the assistant
                # message + usage are already committed (consistent with the
                # text-path retry), so inject feedback and loop.
                if after_action == "continue":
                    async for event in _gate_retry_tail(
                        session, run_id=run_id, feedback=after_feedback or ""
                    ):
                        yield event
                    await _end_active_turn()
                    await _save_checkpoint("turn_complete", turn_index=turn_index)
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
                async for event in _finalize_text_answer(turn_index, assembly):
                    yield event
                if _final_result is not None:
                    return
                continue

            tool_blocks = [b for b in assembly.message.content if isinstance(b, ToolUseBlock)]
            completed_tool_results = await _recover_completed_tool_results(
                session,
                run_id,
                checkpoint.completed_tool_results
                if resume_checkpoint is not None and turn_index == checkpoint.turn_index
                else {},
            )
            _recovery_hints: dict[str, str] = {}
            missing_tool_blocks = [
                block for block in tool_blocks if block.id not in completed_tool_results
            ]
            await _save_checkpoint(
                "tool_batch_pending",
                turn_index=turn_index,
                assistant_message=assembly.message,
                assistant_stop_reason=assembly.stop_reason,
                pending_tool_blocks=tool_blocks,
                completed_tool_results=completed_tool_results,
            )
            async for event in execute_tool_calls(
                missing_tool_blocks,
                agent,
                session,
                signal,
                turn_index=turn_index,
            ):
                await _persist_event(session, run_id, event)
                if isinstance(event, PermissionRequestEvent):
                    await _save_checkpoint(
                        "permission_pending",
                        turn_index=turn_index,
                        assistant_message=assembly.message,
                        assistant_stop_reason=assembly.stop_reason,
                        pending_tool_blocks=tool_blocks,
                        completed_tool_results=completed_tool_results,
                        status="waiting_permission",
                    )
                elif isinstance(event, ToolCallStartEvent):
                    # Persist a placeholder result the moment a tool starts (after
                    # resolve() has fired, Seam B) so a started-but-unfinished tool
                    # is not blindly re-run on resume and the permission_decisions
                    # survive a crash in this window.
                    completed_tool_results.setdefault(
                        event.tool_use_id,
                        _interrupted_tool_result_block(event),
                    )
                    await _save_checkpoint(
                        "tool_executing",
                        turn_index=turn_index,
                        assistant_message=assembly.message,
                        assistant_stop_reason=assembly.stop_reason,
                        pending_tool_blocks=tool_blocks,
                        completed_tool_results=completed_tool_results,
                    )
                elif isinstance(event, ToolCallEndEvent):
                    block = _tool_result_block_from_end(event)
                    completed_tool_results[event.tool_use_id] = block
                    if (
                        event.is_error
                        and event.tool_result is not None
                        and event.tool_result.recovery_hint
                    ):
                        _recovery_hints[event.tool_use_id] = event.tool_result.recovery_hint
                    await _save_checkpoint(
                        "tool_executing",
                        turn_index=turn_index,
                        assistant_message=assembly.message,
                        assistant_stop_reason=assembly.stop_reason,
                        pending_tool_blocks=tool_blocks,
                        completed_tool_results=completed_tool_results,
                    )
                yield event
                if isinstance(event, ToolCallStartEvent):
                    await _dispatch_lifecycle(
                        HookEvent.TOOL_USE_START,
                        ToolUseStartContext(
                            session=session,
                            run_id=run_id,
                            turn_index=turn_index,
                            deps=getattr(session, "run_deps", None),
                            tool_use_id=event.tool_use_id,
                            tool_name=event.tool_name,
                            input=event.input,
                            summary=event.summary,
                        ),
                    )
                elif isinstance(event, ToolCallEndEvent):
                    await _dispatch_lifecycle(
                        HookEvent.TOOL_USE_STOP,
                        ToolUseStopContext(
                            session=session,
                            run_id=run_id,
                            turn_index=turn_index,
                            deps=getattr(session, "run_deps", None),
                            tool_use_id=event.tool_use_id,
                            tool_name=event.tool_name,
                            is_error=event.is_error,
                            duration_ms=event.duration_ms,
                            result=event.result,
                            tool_result=event.tool_result,
                        ),
                    )
            result_blocks: list[ContentBlock] = [
                completed_tool_results[block.id] for block in tool_blocks
            ]
            # Feature E — inject recovery hint when ALL tools in the batch failed.
            # Partial failures are left to the model to resolve on its own.
            if (
                result_blocks
                and all(
                    getattr(b, "type", None) == "tool_result" and getattr(b, "is_error", False)
                    for b in result_blocks
                )
                and _recovery_hints
            ):
                _hint_text = "All tool calls failed. Recovery hints:\n" + "\n".join(
                    f"- {hint}" for hint in _recovery_hints.values()
                )
                _hint_msg = Message(role="user", content=[TextBlock(text=_hint_text)])
                await session.append([_hint_msg])
                _hint_event: Event = UserEvent(message=_hint_msg, subtype="notification")
                await _persist_event(session, run_id, _hint_event)
                yield _hint_event
            result_message = Message(role="user", content=result_blocks)
            already_appended = (
                resume_checkpoint is not None
                and turn_index == checkpoint.turn_index
                and _last_message_has_tool_results(session, tool_blocks)
            )
            if not already_appended:
                await session.append([result_message])
            await _save_checkpoint(
                "tool_results_appended",
                turn_index=turn_index,
                assistant_message=assembly.message,
                assistant_stop_reason=assembly.stop_reason,
                pending_tool_blocks=tool_blocks,
                completed_tool_results=completed_tool_results,
            )
            event = UserEvent(message=result_message, subtype="tool_result")
            await _persist_event(session, run_id, event)
            yield event

            # ── Loop guard evaluation ─────────────────────────────────────
            if _guard is not None and _guard_state is not None:
                _decision = evaluate_loop_guard(_guard, _guard_state, tool_blocks, result_blocks)
                if _decision.action != "continue":
                    event = LoopGuardEvent(
                        reason=_decision.reason,
                        detail=_decision.detail,
                        action=_decision.action,
                    )
                    await _persist_event(session, run_id, event)
                    yield event
                    if _decision.action == "force_final":
                        # Inject a reminder so the model knows to summarise
                        # without further tool calls, then let the loop run
                        # one more tools-disabled turn.
                        from ..skills.system_reminder import wrap_in_system_reminder

                        _reminder_text = wrap_in_system_reminder(
                            "You appear to be stuck in a loop or repeatedly "
                            "encountering failures. Please provide your final "
                            "answer now without making further tool calls."
                        )
                        _reminder_msg = Message(
                            role="user", content=[TextBlock(text=_reminder_text)]
                        )
                        await session.append([_reminder_msg])
                        _force_final_pending = True
                        await _save_checkpoint("turn_complete", turn_index=turn_index)
                        event = UserEvent(message=_reminder_msg, subtype="notification")
                        await _persist_event(session, run_id, event)
                        yield event
                    else:
                        # Hard stop — emit error result and exit.
                        async for event in _emit_error_terminal():
                            yield event
                        return
            # Natural end of turn body (guard said "continue" or "force_final").
            await _end_active_turn()
            await _save_checkpoint("turn_complete", turn_index=turn_index)

        # ── Max-turns exhausted ───────────────────────────────────────────
        _dur = int((time.time() - started) * 1000)
        _final_result = _build_run_result("error", "error", duration_ms=_dur)
        async for event in _max_turns_tail(
            session,
            agent,
            run_id=run_id,
            run_record=run_record,
            checkpoint=checkpoint,
            max_turns=max_turns,
            total=total,
            duration_ms=_dur,
            running_cost=running_cost,
        ):
            yield event
    except AbortError:
        await _cancel_background_workers(session)
        _dur = int((time.time() - started) * 1000)
        _final_result = _build_run_result("aborted", "error", duration_ms=_dur)
        await _close_active_observer_spans(stop_reason="error")
        event = ResultEvent(
            subtype="aborted",
            stop_reason="error",
            total_usage=total,
            duration_ms=_dur,
            total_cost_usd=running_cost,
        )
        await _persist_event(session, run_id, event)
        store = agent.run_store
        if run_record is not None and store is not None:
            checkpoint.phase = "aborted"
            checkpoint.total_usage = total
            await store.save_checkpoint(run_id, checkpoint, status="aborted")
        yield event
    except Exception as exc:
        await _cancel_background_workers(session)
        retryable = getattr(exc, "retryable", False)
        status = getattr(exc, "status", None)
        _err_dict: dict[str, object] = {
            "name": exc.__class__.__name__,
            "message": str(exc),
            "retryable": retryable,
            **({"status": status} if isinstance(status, int) else {}),
        }
        _dur = int((time.time() - started) * 1000)
        _final_result = _build_run_result("error", "error", duration_ms=_dur, error=_err_dict)
        await _close_active_observer_spans(stop_reason="error")
        event = ErrorEvent(error=_err_dict)
        await _persist_event(session, run_id, event)
        yield event
        event = ResultEvent(
            subtype="error",
            stop_reason="error",
            total_usage=total,
            duration_ms=_dur,
            total_cost_usd=running_cost,
        )
        await _persist_event(session, run_id, event)
        store = agent.run_store
        if run_record is not None and store is not None:
            checkpoint.total_usage = total
            await store.mark_failed(run_id, checkpoint, error=_err_dict)
        yield event
    finally:
        # Release the merged-signal watcher when this run created one
        # (opts.signal merged with the session controller); a plain session
        # controller has no watcher and close() is a no-op.
        if signal is not session._abort_controller:
            signal.close()
        await _close_active_observer_spans(stop_reason="error")
        _run_result = _final_result or _build_run_result(
            "error", "error", duration_ms=int((time.time() - started) * 1000)
        )
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
