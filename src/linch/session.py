from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from .abort import AbortContext
from .errors import ConfigError
from .events import Event, ResultEvent
from .run_store import RunLease, _preflight_leased_store, durable_call
from .session_log import SessionLog
from .sessions import SessionStore
from .tools import FileReadTracker
from .types import InvokedSkillRecord, Message, SkillOverlay, Usage

if TYPE_CHECKING:
    from .agent import Agent
    from .permissions import Principal
    from .profiles import Profile
    from .tools import ToolRegistry


_OPEN = "open"
_QUIESCING = "quiescing"
_CLOSED = "closed"


def _set_event() -> asyncio.Event:
    event = asyncio.Event()
    event.set()
    return event


class _BackgroundTasks(list[Any]):
    """Keep the scheduler's append API while retaining only outstanding tasks."""

    def append(self, task: Any) -> None:
        super().append(task)
        if isinstance(task, asyncio.Task):
            task.add_done_callback(self._discard_done)

    def _discard_done(self, task: asyncio.Task[Any]) -> None:
        try:
            self.remove(task)
        except ValueError:
            # Teardown or an explicit owner may already have cleared the list.
            pass


@dataclass(slots=True)
class RunOptions:
    """Override agent defaults for one run or resume operation.

    Attributes:
        stream_partials: Tri-state override where ``None`` preserves the agent default.
        output_schema: Structured-output schema replacing the agent default when set.
        tool_choice: Tool-choice policy replacing the agent default when set.
        final_tool_name: Terminal tool whose input becomes the structured answer.
        deps: Application dependency object visible to this run's tools.
        budget: Spending cap shared with child runs in the subagent tree.
        principal: Immutable identity used by permission evaluation.
        profile: Versioned declarative defaults pinned by the run contract.
        lease: Ephemeral run-store fencing token for this execution.

    Args:
        signal: Abort signal forwarded to the run loop.
        max_output_tokens: Per-run output token cap.
        temperature: Provider sampling temperature override.
        stream_partials: Whether to emit partial assistant events.
        images: Image blocks attached to the user prompt.
        thinking: Provider thinking configuration override.
        effort: Provider reasoning effort override.
        output_schema: Structured-output schema override.
        tool_choice: Tool-choice policy override.
        final_tool_name: Tool whose call supplies the final structured answer.
        deps: Per-run dependency object for tools.
        budget: Per-run spending budget.
        run_id: Pending run identifier to adopt when starting a queued run.
        lease: Immutable run-store lease that fences this run's writes.
        principal: Tenant, user, and role identity for authorization.
        profile: Versioned profile supplying run-scoped defaults.
    """

    signal: Any = None
    max_output_tokens: int | None = None
    temperature: float | None = None
    stream_partials: bool | None = None
    """Override partial assistant-event emission for this run.

    ``None`` preserves :attr:`Agent.include_partial_messages`; ``False``
    suppresses raw partial events while provider deltas continue to assemble
    into the normal assistant message.
    """
    images: list[dict[str, str]] | None = None
    thinking: dict[str, Any] | None = None
    effort: str | None = None
    output_schema: Any = None  # OutputSchema | None
    """JSON Schema for structured output.  Overrides ``Agent.output_schema``
    for this run when set.  See :class:`~linch.types.OutputSchema`."""
    tool_choice: Any = None  # ToolChoice | None
    """Tool-choice override for this run.  See ``linch.types.ToolChoice``."""
    final_tool_name: str | None = None
    """Name of a tool whose invocation terminates the loop and sets
    ``ResultEvent.structured_output`` from the tool-use input.  Overrides
    ``Agent.final_tool_name``."""
    deps: Any = None
    """Per-run dependency object passed into :attr:`ToolContext.deps`.
    Overrides ``Agent.deps`` when set to a non-``None`` value."""
    budget: Any = None  # RunBudget | None
    """Spending cap for this run (and its subagent tree).  Overrides
    ``Agent.budget`` when set.  See :class:`~linch.budget.RunBudget`."""

    run_id: str | None = None
    """Adopt this pending run ID when starting a queued run."""

    lease: RunLease | None = None
    """Immutable run-store lease used to fence this execution's writes."""

    principal: Principal | None = None
    """Immutable tenant/user/role identity for this run's authorization."""

    profile: Profile | None = None
    """Versioned run defaults; pass the same definition again when resuming."""


@dataclass(slots=True)
class AlignmentEntry:
    prompt: str
    images: list[dict[str, str]] | None
    future: asyncio.Future[None]
    cancelled: bool = False
    applying: bool = False


@dataclass(slots=True)
class Session:
    """Hold one conversation and its durable append-only message log.

    Args:
        id: Stable session identifier.
        created_at: Store-provided creation timestamp.
        meta: JSON-compatible session metadata.
        agent: Agent that owns this session's runtime services.
        store: Session store used for durable message and metadata writes.
        session_log: Append-only in-memory log backing the provider and audit views.
        _extensions: Checkpointed JSON-only extension state.
        _active: Whether a run is currently admitted.
        _closed: Whether this session has completed teardown.
        _lifecycle_state: Internal lifecycle state.
        _close_task: In-flight session teardown task.
        _idle: Event set after run and finalizer cleanup complete.
        _telemetry_run: Run telemetry handle, when active.
        active_run_id: Identifier of the currently admitted run.
        _active_principal: Principal captured for the active run.
        _profile_cleanup: Cleanup callback for the active profile scope.
        _active_gen: Outer async generator driving the active run.
        _active_inner: Underlying loop async iterator.
        _active_started: Whether the outer run generator has started.
        _event_journal: Buffered durable event journal for the active run.
        _model_call_ledger: Receipt ledger for model calls in the active run.
        _last_seq: Highest stored-message sequence observed.
        _seq_cacheable: Whether sequence order permits snapshot caching.
        _last_prompt_cache_model: Model used for the previous provider call.
        _last_prompt_cache_tool_sig: Tool signature from the previous provider call.
        alignment_queue: Steering messages waiting for a turn boundary.
        interrupt_requested: Whether cooperative interruption was requested.
        last_usage: Most recent provider usage record.
        last_compaction_info: Metadata from the most recent compaction.
        active_budget: Budget resolved for the active run.
        inherited_budget: Budget inherited from a parent run.
        compaction_retry_used_this_turn: Whether this turn consumed its compaction retry.
        active_model: Model override selected during the active run.
        fallback_index: Number of fallback models consumed.
        pending_skill_overlay: Skill prompt overlay waiting to apply.
        current_turn_allowed_tools: Tools allowed in the current turn.
        current_turn_permission_decisions: Permission decisions for the current turn.
        invoked_skills: Skills invoked in this session.
        skills_loaded_emitted: Whether the skills-loaded event was emitted.
        tools_override: Session-specific tool registry override.
        system_blocks_override: Session-specific system blocks.
        file_read_tracker: Files read by tools in this session.
        _abort_controller: Abort controller for the active run.
        run_deps: Dependencies resolved for the active run.
        filesystem: Session virtual filesystem backend.
        workers: Retained child-worker handles.
        pending_child_events: Child events waiting for host delivery.
        pending_notifications: Background-worker notifications waiting for delivery.
        background_tasks: Detached tasks owned by the session.
        mailbox_address: Optional inbox address for peer messages.
        cwd_override: Session working-directory override.

    Attributes:
        active_run_id: Identifier of the currently admitted run, when any.
        pending_notifications: In-process user messages waiting for the next turn.
        background_tasks: Detached tool tasks owned by the session.
        workers: Retained child-worker handles keyed by worker id.
    """

    id: str
    created_at: str
    meta: dict[str, object]
    agent: Agent
    store: SessionStore
    session_log: SessionLog = field(default_factory=SessionLog)
    """The single append-only log behind the session-owned provider view and
    audit history. Ephemeral per-request context is assembled outside this log.
    Compaction is recorded as a projection, never an untracked mutation."""
    _extensions: dict[str, Any] = field(default_factory=dict, repr=False)
    """JSON-only extension state captured in the run checkpoint."""
    _active: bool = False
    _closed: bool = False
    _lifecycle_state: str = _OPEN
    _close_task: asyncio.Task[None] | None = None
    _idle: asyncio.Event = field(default_factory=_set_event)
    """Set only when the run iterator and all of its finalizers are finished."""
    _telemetry_run: Any = field(default=None, repr=False)
    active_run_id: str | None = None
    _active_principal: Principal | None = field(default=None, repr=False)
    _run_bindings: dict[str, Any] = field(default_factory=dict, repr=False)
    _config_seed: Any = field(default=None, repr=False)
    _active_gen: Any = None
    """The async generator driving the in-flight ``run``/``resume`` iterator.
    Captured so a forced ``aclose`` can finalize an abandoned run generator
    (running its ``finally``) instead of leaving ``_active`` stuck true."""
    _active_inner: Any = None
    """The underlying loop iterator, retained for never-started generator cleanup."""
    _active_started: bool = False
    """Whether ``_iterate`` has entered its body for the admitted run."""
    _event_journal: Any = None
    """Per-run ``RunEventBuffer`` (loop/checkpoint.py) that batches observational
    events between flush points. Set by the runner at run start, cleared in its
    finally. ``None`` outside an active durable run."""
    _model_call_ledger: Any = None
    """Run-bound receipt ledger, installed by the loop and cleared on exit."""
    _last_seq: int = 0
    """Highest stored-message ``seq`` observed (the provider-view snapshot
    watermark). Tracks the real seq returned by the store, so gaps are tolerated."""
    _seq_cacheable: bool = True
    """Whether stored-message seqs have stayed strictly increasing. Non-increasing
    or non-int seqs disable provider-view snapshot caching for this session."""
    _last_prompt_cache_model: str | None = None
    """Model of the previous provider call; compared each turn to flag a
    cache-prefix-breaking model change (``PromptCacheAdvisoryEvent``)."""
    _last_prompt_cache_tool_sig: Any = None
    """Tool signature (`_prompt_cache.tool_signature`) of the previous provider
    call; compared each turn to flag a cache-prefix-breaking tool-set change."""
    alignment_queue: list[AlignmentEntry] = field(default_factory=list)
    interrupt_requested: bool = False
    last_usage: Usage | None = None
    last_compaction_info: dict[str, Any] | None = None
    active_budget: Any = None  # RunBudget | None
    """The resolved budget for the in-flight run.  Set by ``run_loop`` from
    ``RunOptions.budget`` → ``inherited_budget`` → ``Agent.budget``; read by
    ``run_subagent`` so child sessions join the parent's budget."""
    inherited_budget: Any = None  # RunBudget | None
    """Budget inherited from a parent session (set on subagent child sessions
    by ``run_subagent``).  Same object as the parent's ``active_budget``."""
    compaction_retry_used_this_turn: bool = False
    active_model: str | None = None
    """Run-level model override set by the model-fallback recovery path when the
    primary model overloads. ``None`` means use ``agent.model``. Reset at the
    start of each run."""
    fallback_index: int = 0
    """How many entries of ``agent.fallback_models`` have been consumed this run."""
    pending_skill_overlay: SkillOverlay | None = None
    current_turn_allowed_tools: list[str] | None = None
    current_turn_permission_decisions: dict[str, dict] = field(default_factory=dict)
    invoked_skills: list[InvokedSkillRecord] = field(default_factory=list)
    skills_loaded_emitted: bool = False
    tools_override: ToolRegistry | None = None
    system_blocks_override: Any = None
    file_read_tracker: FileReadTracker = field(default_factory=FileReadTracker)
    _abort_controller: AbortContext = field(default_factory=AbortContext)
    run_deps: Any = None
    """Resolved dependency object for the current run.  Set by ``run_loop``
    from ``RunOptions.deps`` (falling back to ``Agent.deps``) and threaded
    into :attr:`~linch.tools.ToolContext.deps` via the scheduler."""
    filesystem: Any = None
    """Per-session virtual filesystem backend (:class:`~linch.filesystem.backend.FileBackend`).
    Threaded into :attr:`~linch.tools.base.ToolContext.filesystem` on every
    tool call.  ``None`` when the filesystem subsystem is disabled."""
    workers: dict[str, Any] = field(default_factory=dict)
    """Live in-process worker handles keyed by worker_id.

    Populated only for retained deep-agent subagents. These handles are not
    restart-durable; after process restart, spawn a new worker.
    """
    pending_child_events: list[Event] = field(default_factory=list)
    """SubagentEvents accumulated by in-flight child sessions; available to host UIs."""
    pending_notifications: list[Message] = field(default_factory=list)
    """In-process background-worker <task-notification> messages, drained next turn."""
    background_tasks: list[Any] = field(default_factory=_BackgroundTasks)
    """Detached asyncio.Tasks for backgrounded tool calls (run_in_background hint).
    Removed on completion and cancelled/drained on abort or close."""
    mailbox_address: str | None = None
    """This session's inbox address on ``agent.mailbox``; drained into provider_view
    each turn when set. Workers default to their display_name; the root is set by
    the embedder. ``None`` means this session does not receive peer messages."""
    cwd_override: str | None = None
    """Per-session working directory overriding ``agent.cwd`` for tool execution and
    permission path-rule matching. Set by an :class:`~linch.tools.isolation.IsolationBackend`
    so a subagent branch runs in its own cwd. ``None`` = use ``agent.cwd``."""

    @property
    def provider_view(self) -> Sequence[Message]:
        """Return a read-only snapshot of the session-owned provider projection.

        Returns:
            Detached model-visible messages in provider order.
        """
        return self.session_log.provider_view

    @property
    def full_history(self) -> Sequence[Message]:
        """Return a read-only snapshot of the session's full audit history.

        Returns:
            Detached historical messages in append order.
        """
        return self.session_log.full_history

    @property
    def message_count(self) -> int:
        """Return the number of messages in the model-visible projection.

        Returns:
            Number of messages currently visible to the model.
        """
        return self.session_log.visible_count

    def last_provider_message(self) -> Message | None:
        """The newest model-visible message, or ``None`` when the view is empty.

        Prefer this over ``session.provider_view[-1]``: it copies one message
        instead of snapshotting the whole conversation.

        Returns:
            The newest model-visible message, or ``None`` when the view is empty.
        """
        return self.session_log.last_visible()

    def run(self, prompt: str, opts: RunOptions | None = None) -> AsyncIterator[Event]:
        """Start a new run and return its event stream.

        Args:
            prompt: User text that starts the run.
            opts: Optional per-run overrides.

        Returns:
            An asynchronous iterator yielding typed run events.

        Raises:
            ConfigError: If the session or owning agent is closed, or a run is active.
        """
        if self._lifecycle_state != _OPEN or self._closed:
            raise ConfigError("session is closed")
        if getattr(self.agent, "_lifecycle_state", _OPEN) != _OPEN:
            raise ConfigError("agent is closing")
        if self._active:
            raise ConfigError("Session already has an active run")
        options = replace(
            opts or RunOptions(), run_id=(opts.run_id if opts else None) or str(uuid4())
        )
        _preflight_leased_store(
            self.agent.run_store,
            options.run_id or "",
            options.lease,
            include_snapshots=bool(
                getattr(getattr(self.agent, "durability", None), "exact_model_input", False)
            ),
        )
        self._active = True
        self._active_started = False
        self._idle.clear()
        self.interrupt_requested = False
        self._abort_controller = AbortContext()

        from .loop import run_loop

        inner = run_loop(self, prompt, options, _adopt_run_id=bool(opts and opts.run_id))
        self._active_inner = inner
        it = self._iterate(inner, "", options)
        self._active_gen = it
        return it

    def resume(self, run_id: str, opts: RunOptions | None = None) -> AsyncIterator[Event]:
        """Resume a durable run and return its event stream.

        Args:
            run_id: Durable run identifier to resume.
            opts: Optional per-run overrides, including a pinned profile.

        Returns:
            An asynchronous iterator yielding resumed run events.

        Raises:
            ConfigError: If the session is unavailable, already running, or has no run store.
        """
        if self._lifecycle_state != _OPEN or self._closed:
            raise ConfigError("session is closed")
        if getattr(self.agent, "_lifecycle_state", _OPEN) != _OPEN:
            raise ConfigError("agent is closing")
        if self._active:
            raise ConfigError("Session already has an active run")
        if self.agent.run_store is None:
            raise ConfigError("Agent has no run_store configured")
        options = replace(opts or RunOptions(), run_id=run_id)
        _preflight_leased_store(
            self.agent.run_store,
            options.run_id or "",
            options.lease,
            include_snapshots=bool(
                getattr(getattr(self.agent, "durability", None), "exact_model_input", False)
            ),
        )
        self._active = True
        self._active_started = False
        self._idle.clear()
        self.interrupt_requested = False
        self._abort_controller = AbortContext()

        from .loop import resume_loop

        inner = resume_loop(self, run_id, options)
        self._active_inner = inner
        it = self._iterate(inner, run_id, options)
        self._active_gen = it
        return it

    async def _iterate(
        self,
        inner: AsyncIterator[Event],
        run_id_fallback: str,
        opts: RunOptions,
    ) -> AsyncIterator[Event]:
        from .hooks import EventEmitContext, HookDispatcher
        from .lifecycle import EVENT_EMIT

        self._active_started = True
        finished = False

        async def finish() -> None:
            nonlocal finished
            if finished:
                return
            finished = True
            # Resume once past the terminal event so the loop wrapper can run
            # its own ``async with aclosing(...)`` exit path synchronously. A
            # direct ``aclose`` here schedules nested async-generator finalizers
            # that can otherwise linger as pending tasks during rapid churn.
            try:
                await anext(inner)
            except StopAsyncIteration:
                pass
            else:
                await _aclose_generator(inner)
            finally:
                try:
                    if scope is not None:
                        await scope.__aexit__(None, None, None)
                finally:
                    self._finish_run()
                    if self.agent.max_idle_sessions is not None:
                        await self.agent._session_became_idle(self)

        scope = None
        try:
            from .profiles import _profile_scope

            assert opts.run_id is not None
            pending_scope = _profile_scope(self, opts.profile, opts.run_id)
            config = await pending_scope.__aenter__()
            scope = pending_scope
            hooks = HookDispatcher(config.hooks)
            async for event in inner:
                if isinstance(event, ResultEvent):
                    # Async generators are not automatically closed when a
                    # consumer breaks from ``async for``. Complete the inner
                    # loop and release admission before handing the terminal
                    # event to the caller, so the session cannot remain parked
                    # while the caller retains the stream object.
                    event_run_id = self.active_run_id or run_id_fallback
                    event_deps = getattr(self, "run_deps", None)
                    if hooks.active:
                        await hooks.dispatch(
                            EVENT_EMIT,
                            EventEmitContext(
                                session=self,
                                run_id=event_run_id,
                                turn_index=None,
                                deps=event_deps,
                                event=event,
                            ),
                        )
                    await finish()
                    yield event
                    continue
                yield event
                if hooks.active:
                    await hooks.dispatch(
                        EVENT_EMIT,
                        EventEmitContext(
                            session=self,
                            run_id=self.active_run_id or run_id_fallback,
                            turn_index=None,
                            deps=getattr(self, "run_deps", None),
                            event=event,
                        ),
                    )
        finally:
            try:
                if not finished:
                    await _aclose_generator(inner)
            finally:
                try:
                    if scope is not None and not finished:
                        await scope.__aexit__(None, None, None)
                finally:
                    if not finished:
                        self._finish_run()
                        if self.agent.max_idle_sessions is not None:
                            await self.agent._session_became_idle(self)

    def _finish_run(self) -> None:
        """Release the single-run admission token and publish true idleness."""
        self._active_principal = None
        self._active_gen = None
        self._active_inner = None
        self._active_started = False
        self._reject_pending_alignment(ConfigError("run ended before alignment was applied"))
        self._active = False
        self.active_run_id = None
        self._model_call_ledger = None
        self._idle.set()

    async def align(
        self,
        prompt: str,
        *,
        images: list[dict[str, str]] | None = None,
        timeout_s: float | None = None,
    ) -> None:
        """Queue steering text for the next boundary of an active run.

        Args:
            prompt: Non-empty user message to inject before the next provider call.
            images: Optional image blocks to attach to the injected message.
            timeout_s: Maximum wait for delivery; ``None`` waits without a deadline.

        Raises:
            ConfigError: If no run is active, the session is closed, the prompt is empty,
                or delivery misses the deadline.
        """
        if self._lifecycle_state != _OPEN:
            raise ConfigError("session is closed")
        if not self._active:
            raise ConfigError("Session has no active run to align")
        if not isinstance(prompt, str) or prompt == "":
            raise ConfigError("alignment prompt must be a non-empty string")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()
        entry = AlignmentEntry(prompt=prompt, images=images, future=future)
        self.alignment_queue.append(entry)
        if timeout_s is None:
            await future
            return
        try:
            await asyncio.wait_for(asyncio.shield(future), timeout_s)
        except TimeoutError:
            # Mark the entry before removing it from the pending queue. The
            # drain may already be running a slow hook; it checks this marker
            # before appending so a timed-out alignment cannot be applied after
            # the caller has been told it was rejected. If durable append has
            # begun, wait for its truthful completion instead of reporting a
            # timeout for a message that is already being committed.
            entry.cancelled = True
            if entry.applying:
                await future
                return
            if entry in self.alignment_queue:
                self.alignment_queue.remove(entry)
            if not future.done():
                future.cancel()
            raise ConfigError("alignment timed out before it was applied") from None

    def interrupt(self) -> None:
        """Request a cooperative stop at the next loop boundary."""
        self.interrupt_requested = True

    def abort(self) -> None:
        """Abort the active run and cancel owned worker and background tasks."""
        self._abort_controller.abort()
        # Cancel any running background worker tasks so they don't write into
        # a dead session after the run ends.
        for handle in self.workers.values():
            task = getattr(handle, "task", None)
            if task is not None and isinstance(task, asyncio.Task) and not task.done():
                task.cancel()
        # Cancel detached background-tool tasks too.
        for task in self.background_tasks:
            if isinstance(task, asyncio.Task) and not task.done():
                task.cancel()
        self._reject_pending_alignment(ConfigError("run aborted before alignment was applied"))

    def _reject_pending_alignment(self, exc: Exception) -> None:
        entries = list(self.alignment_queue)
        self.alignment_queue.clear()
        for entry in entries:
            if not entry.future.done():
                entry.future.set_exception(exc)

    async def _drain_owned_work(self) -> None:
        """Cancel this session's owned worker/background tasks and await them so
        their finalizers run before any resource they touch is closed."""
        tasks: list[asyncio.Task[Any]] = []
        for handle in self.workers.values():
            task = getattr(handle, "task", None)
            if isinstance(task, asyncio.Task):
                tasks.append(task)
        for task in self.background_tasks:
            if isinstance(task, asyncio.Task):
                tasks.append(task)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.background_tasks.clear()

    async def aclose(self, force: bool = False) -> None:
        """Release this session's live resources and unregister it from the agent.

        Idempotent. Removes the in-memory registration without deleting durable
        history in the session store. Rejects a session with an active run unless
        *force* is set; a forced release aborts the run, drains owned background
        work so finalizers run, and recursively releases retained child sessions.

        Args:
            force: Whether to abort an active run and drain its work instead of
                raising. Required to release a session whose run has not ended.
        """
        if self._lifecycle_state == _CLOSED or self._closed:
            return
        if self._active and not force:
            raise ConfigError(
                "session has an active run; call aclose(force=True) to abort and release it"
            )
        self._lifecycle_state = _QUIESCING
        task = self._close_task
        if task is None or task.done():
            # A failed teardown deliberately leaves the session quiescing. A
            # later aclose() starts a fresh attempt and retries the unfinished
            # portion instead of publishing CLOSED prematurely.
            task = asyncio.create_task(self._aclose_impl(force=force))
            task.add_done_callback(_consume_task_exception)
            self._close_task = task
        await asyncio.shield(task)

    async def _aclose_impl(self, *, force: bool) -> None:
        if self._active:
            if not force:
                raise ConfigError(
                    "session has an active run; call aclose(force=True) to abort and release it"
                )
            self.abort()
            await self._finalize_active_run()
        await self._drain_owned_work()
        # Recursively release retained child sessions owned by this session
        # (snapshot ids first — release mutates the agent's registry).
        for worker_id, handle in list(self.workers.items()):
            child_id = getattr(handle, "child_session_id", None)
            if not isinstance(child_id, str) or not child_id:
                continue
            await self.agent.release_session(child_id, force=True)
            # Remove each ownership edge only after that child successfully
            # closes. A failing child teardown therefore remains discoverable
            # and retryable on the next parent aclose().
            self.workers.pop(worker_id, None)
        self.workers.clear()
        # Keep the Session seed alive until nested run cleanup has removed its
        # parent-owned effect; its final seed release must not await itself.
        for binding in tuple(self._run_bindings.values()):
            if binding.retains == 0:
                await binding.release()
        if self._config_seed is not None:
            seed = self._config_seed
            await seed.release()
            self._config_seed = None
        self._lifecycle_state = _CLOSED
        self._closed = True
        self.agent._unregister_session(self.id, self)

    async def _finalize_active_run(self) -> None:
        """Abort an admitted run and wait until its complete unwind is observable.

        Async generators that have never been iterated do not execute their body
        or ``finally`` when closed. Handle that case explicitly so the admission
        token and idle barrier cannot remain stuck. If another task is currently
        advancing the generator, the abort signal lets that task unwind and this
        method waits for its finalizer rather than closing shared resources early.
        """
        gen = self._active_gen
        if gen is None:
            self._finish_run()
            return

        if not self._active_started:
            # Neither async-generator body has started, so closing them cannot
            # run ``_iterate``'s finally. Only publish idle after both closes
            # succeed; a teardown error remains retryable.
            await _aclose_generator(gen)
            inner = self._active_inner
            if inner is not None:
                await _aclose_generator(inner)
            self._finish_run()
            return

        if getattr(gen, "ag_running", False):
            await self._idle.wait()
            return
        try:
            await _aclose_generator(gen)
        except RuntimeError:
            # ``ag_running`` can change between the check and aclose(). Only
            # reinterpret the error as that race while the run is still active;
            # genuine teardown RuntimeErrors remain visible to the caller.
            if not self._active:
                raise
            await self._idle.wait()
            return
        await self._idle.wait()

    async def __aenter__(self) -> Session:
        """Return this session for use in an async context manager.

        Returns:
            This live session.
        """
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Force-release the session when leaving an async context manager.

        Args:
            exc: Exception details supplied by the context-manager protocol.
        """
        await self.aclose(force=True)

    def mark_compaction_used(self) -> None:
        """Mark the current turn as having consumed its compaction retry."""
        self.compaction_retry_used_this_turn = True

    async def notify(
        self,
        message: Message,
        delivery_id: str | None = None,
        source: str = "host",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Queue a user message for insertion at the next turn boundary.

        The default path preserves the legacy in-process notification queue.
        With durable inbox enabled, the delivery is written to the optional
        session-store capability and deduplicated by its stable delivery id.

        Args:
            message: User-authored message to deliver at a turn boundary.
            delivery_id: Stable id used for durable deduplication; generated when omitted.
            source: Host-defined origin label for durable delivery metadata.
            metadata: JSON-compatible delivery metadata.

        Returns:
            The stable delivery id assigned to the queued message.

        Raises:
            ConfigError: If the session is closed, the message is not a user message,
                or durable inbox support is unavailable.
        """
        if self._lifecycle_state == _CLOSED or self._closed:
            raise ConfigError("session is closed")
        if not isinstance(message, Message) or message.role != "user":
            raise ConfigError("Session.notify() requires a user Message")
        stable_id = delivery_id or uuid4().hex
        durability = getattr(self.agent, "durability", None)
        if not getattr(durability, "durable_inbox", False):
            self.pending_notifications.append(message)
            return stable_id

        enqueue = getattr(self.store, "enqueue_inbox", None)
        commit = getattr(self.store, "commit_inbox", None)
        if not callable(enqueue) or not callable(commit):
            raise ConfigError(
                "durable_inbox requires a SessionStore implementing "
                "enqueue_inbox() and commit_inbox()"
            )
        from .durability import InboxDelivery

        await durable_call(
            self,
            "session.enqueue_inbox",
            cast(Any, enqueue)(
                self.id,
                InboxDelivery(
                    delivery_id=stable_id,
                    message=message,
                    source=source,
                    metadata=dict(metadata or {}),
                ),
            ),
        )
        return stable_id

    async def append(self, messages: list[Message]) -> None:
        """Durably append messages, then update the in-memory session log.

        Args:
            messages: Ordered messages to append to both the store and log.

        Raises:
            ConfigError: If the session has been closed.
        """
        # An admitted run may still need to durably append its terminal state
        # while the session is quiescing. Only the final CLOSED transition makes
        # history immutable through this live object.
        if self._lifecycle_state == _CLOSED or self._closed:
            raise ConfigError("session is closed")
        stored = await durable_call(
            self,
            "session.append",
            self.store.append_messages(self.id, messages),
        )
        self._track_seqs(stored)
        self.session_log.append_many(messages)

    def _track_seqs(self, stored: list[Any]) -> None:
        for row in stored:
            seq = getattr(row, "seq", None)
            if isinstance(seq, int) and seq > self._last_seq:
                self._last_seq = seq  # gaps (a forward jump) are fine
            else:
                # Non-increasing or non-int seq: the snapshot watermark can no
                # longer be trusted, so disable caching for this session.
                self._seq_cacheable = False
                if isinstance(seq, int):
                    self._last_seq = max(self._last_seq, seq)

    async def update_meta(self, patch: dict[str, object]) -> None:
        """Persist and merge a metadata patch.

        Args:
            patch: JSON-compatible metadata fields to update.

        Raises:
            ConfigError: If the session has been closed.
        """
        if self._lifecycle_state == _CLOSED or self._closed:
            raise ConfigError("session is closed")
        updated = await durable_call(
            self,
            "session.update_meta",
            self.store.update_meta(self.id, patch),
        )
        self.meta.update(updated.meta)


async def _aclose_generator(gen: Any) -> None:
    """Finalize a duck-typed async iterator without hiding teardown failures."""
    aclose = getattr(gen, "aclose", None)
    if aclose is None:
        return
    await aclose()


def _consume_task_exception(task: asyncio.Task[None]) -> None:
    """Avoid an un-retrieved exception if a shielded caller is cancelled."""
    if not task.cancelled():
        task.exception()
