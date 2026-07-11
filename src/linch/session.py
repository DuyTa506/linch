from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .abort import AbortContext
from .errors import ConfigError
from .events import Event
from .sessions import SessionStore
from .tools import FileReadTracker
from .types import InvokedSkillRecord, Message, SkillOverlay, Usage

if TYPE_CHECKING:
    from .agent import Agent
    from .tools import ToolRegistry


@dataclass(slots=True)
class RunOptions:
    signal: Any = None
    max_output_tokens: int | None = None
    temperature: float | None = None
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


@dataclass(slots=True)
class AlignmentEntry:
    prompt: str
    images: list[dict[str, str]] | None
    future: asyncio.Future[None]


@dataclass(slots=True)
class Session:
    id: str
    created_at: str
    meta: dict[str, object]
    agent: Agent
    store: SessionStore
    provider_view: list[Message] = field(default_factory=list)
    full_history: list[Message] = field(default_factory=list)
    _active: bool = False
    _closed: bool = False
    active_run_id: str | None = None
    _active_gen: Any = None
    """The async generator driving the in-flight ``run``/``resume`` iterator.
    Captured so a forced ``aclose`` can finalize an abandoned run generator
    (running its ``finally``) instead of leaving ``_active`` stuck true."""
    _event_journal: Any = None
    """Per-run ``RunEventBuffer`` (loop/checkpoint.py) that batches observational
    events between flush points. Set by the runner at run start, cleared in its
    finally. ``None`` outside an active durable run."""
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
    background_tasks: list[Any] = field(default_factory=list)
    """Detached asyncio.Tasks for backgrounded tool calls (run_in_background hint).
    Cancelled on abort so they never write into a dead session."""
    mailbox_address: str | None = None
    """This session's inbox address on ``agent.mailbox``; drained into provider_view
    each turn when set. Workers default to their display_name; the root is set by
    the embedder. ``None`` means this session does not receive peer messages."""
    cwd_override: str | None = None
    """Per-session working directory overriding ``agent.cwd`` for tool execution and
    permission path-rule matching. Set by an :class:`~linch.tools.isolation.IsolationBackend`
    so a subagent branch runs in its own cwd. ``None`` = use ``agent.cwd``."""

    @property
    def message_count(self) -> int:
        return len(self.provider_view)

    def run(self, prompt: str, opts: RunOptions | None = None) -> AsyncIterator[Event]:
        if self._closed:
            raise ConfigError("session is closed")
        if self._active:
            raise ConfigError("Session already has an active run")
        self._active = True
        self.interrupt_requested = False
        self._abort_controller = AbortContext()

        from .loop import run_loop

        it = self._iterate(run_loop(self, prompt, opts or RunOptions()), "")
        self._active_gen = it
        return it

    def resume(self, run_id: str, opts: RunOptions | None = None) -> AsyncIterator[Event]:
        if self._closed:
            raise ConfigError("session is closed")
        if self._active:
            raise ConfigError("Session already has an active run")
        if self.agent.run_store is None:
            raise ConfigError("Agent has no run_store configured")
        self._active = True
        self.interrupt_requested = False
        self._abort_controller = AbortContext()

        from .loop import resume_loop

        it = self._iterate(resume_loop(self, run_id, opts or RunOptions()), run_id)
        self._active_gen = it
        return it

    async def _iterate(
        self,
        inner: AsyncIterator[Event],
        run_id_fallback: str,
    ) -> AsyncIterator[Event]:
        from .hooks import EventEmitContext, HookDispatcher, HookEvent

        hooks = HookDispatcher(getattr(self.agent, "hooks", None))
        try:
            async for event in inner:
                yield event
                if hooks.active:
                    await hooks.dispatch(
                        HookEvent.EVENT_EMIT,
                        EventEmitContext(
                            session=self,
                            run_id=self.active_run_id or run_id_fallback,
                            turn_index=None,
                            deps=getattr(self, "run_deps", None),
                            event=event,
                        ),
                    )
        finally:
            await _aclose_quietly(inner)
            self._active_gen = None
            self._reject_pending_alignment(ConfigError("run ended before alignment was applied"))
            self._active = False
            self.active_run_id = None

    async def align(
        self,
        prompt: str,
        *,
        images: list[dict[str, str]] | None = None,
        timeout_s: float | None = None,
    ) -> None:
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
            await asyncio.wait_for(future, timeout_s)
        except asyncio.TimeoutError:
            # The run never reached a alignment boundary in time. Drop the still
            # -pending entry so it cannot inject into a later turn, and surface
            # the timeout instead of blocking the caller indefinitely. (If the
            # drain already removed it, the future is now cancelled and the
            # drain's `future.done()` guard makes its set_result a no-op.)
            if entry in self.alignment_queue:
                self.alignment_queue.remove(entry)
            raise ConfigError("alignment timed out before it was applied") from None

    def interrupt(self) -> None:
        self.interrupt_requested = True

    def abort(self) -> None:
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
        if self._closed:
            return
        if self._active and not force:
            raise ConfigError(
                "session has an active run; call aclose(force=True) to abort and release it"
            )
        if self._active:
            self.abort()
            # Finalize an abandoned run generator so its finally runs (resetting
            # ``_active``). If a consumer is actively iterating it in another task
            # aclose() raises RuntimeError — swallow it; the abort signal ends the
            # run and the consumer's own iteration runs the finally.
            gen = self._active_gen
            if gen is not None:
                await _aclose_quietly(gen)
        await self._drain_owned_work()
        # Recursively release retained child sessions owned by this session
        # (snapshot ids first — release mutates the agent's registry).
        child_ids = [
            cid
            for handle in self.workers.values()
            if isinstance((cid := getattr(handle, "child_session_id", None)), str) and cid
        ]
        self.workers.clear()
        for child_id in child_ids:
            await self.agent.release_session(child_id, force=True)
        self._closed = True
        self.agent._unregister_session(self.id, self)

    async def __aenter__(self) -> Session:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose(force=True)

    def mark_compaction_used(self) -> None:
        self.compaction_retry_used_this_turn = True

    async def append(self, messages: list[Message]) -> None:
        if self._closed:
            raise ConfigError("session is closed")
        stored = await self.store.append_messages(self.id, messages)
        self._track_seqs(stored)
        self.provider_view.extend(messages)
        self.full_history.extend(messages)

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
        if self._closed:
            raise ConfigError("session is closed")
        updated = await self.store.update_meta(self.id, patch)
        self.meta.update(updated.meta)


async def _aclose_quietly(gen: Any) -> None:
    """Finalize an async generator, swallowing the RuntimeError raised when it is
    already running in another task and any error from its own teardown."""
    aclose = getattr(gen, "aclose", None)
    if aclose is None:
        return
    try:
        await aclose()
    except Exception:
        pass
