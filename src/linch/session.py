from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .abort import AbortContext
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
class Session:
    id: str
    created_at: str
    meta: dict[str, object]
    agent: Agent
    store: SessionStore
    provider_view: list[Message] = field(default_factory=list)
    full_history: list[Message] = field(default_factory=list)
    _active: bool = False
    active_run_id: str | None = None
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
    filesystem: Any = None
    workers: dict[str, Any] = field(default_factory=dict)
    """Live in-process worker handles keyed by worker_id.

    Populated only for retained deep-agent subagents. These handles are not
    restart-durable; after process restart, spawn a new worker.
    """
    pending_child_events: list[Event] = field(default_factory=list)
    """SubagentEvents accumulated by in-flight child sessions; available to host UIs."""
    pending_notifications: list[Message] = field(default_factory=list)
    """In-process background-worker <task-notification> messages, drained next turn."""
    """Per-session virtual filesystem backend (:class:`~linch.filesystem.backend.FileBackend`).
    Threaded into :attr:`~linch.tools.base.ToolContext.filesystem` on every
    tool call.  ``None`` when the filesystem subsystem is disabled."""
    """Resolved dependency object for the current run.  Set by ``run_loop``
    from ``RunOptions.deps`` (falling back to ``Agent.deps``) and threaded
    into :attr:`~linch.tools.ToolContext.deps` via the scheduler."""

    @property
    def message_count(self) -> int:
        return len(self.provider_view)

    def run(self, prompt: str, opts: RunOptions | None = None) -> AsyncIterator[Event]:
        if self._active:
            from .errors import ConfigError

            raise ConfigError("Session already has an active run")
        self._active = True
        self._abort_controller = AbortContext()

        async def iterator() -> AsyncIterator[Event]:
            from .hooks import EventEmitContext, HookDispatcher, HookEvent
            from .loop import run_loop

            _hooks = HookDispatcher(getattr(self.agent, "hooks", None))
            try:
                async for event in run_loop(self, prompt, opts or RunOptions()):
                    yield event
                    if _hooks.active:
                        await _hooks.dispatch(
                            HookEvent.EVENT_EMIT,
                            EventEmitContext(
                                session=self,
                                run_id=self.active_run_id or "",
                                turn_index=None,
                                deps=getattr(self, "run_deps", None),
                                event=event,
                            ),
                        )
            finally:
                self._active = False
                self.active_run_id = None

        return iterator()

    def resume(self, run_id: str, opts: RunOptions | None = None) -> AsyncIterator[Event]:
        if self._active:
            from .errors import ConfigError

            raise ConfigError("Session already has an active run")
        if self.agent.run_store is None:
            from .errors import ConfigError

            raise ConfigError("Agent has no run_store configured")
        self._active = True
        self._abort_controller = AbortContext()

        async def iterator() -> AsyncIterator[Event]:
            from .hooks import EventEmitContext, HookDispatcher, HookEvent
            from .loop import resume_loop

            _hooks = HookDispatcher(getattr(self.agent, "hooks", None))
            try:
                async for event in resume_loop(self, run_id, opts or RunOptions()):
                    yield event
                    if _hooks.active:
                        await _hooks.dispatch(
                            HookEvent.EVENT_EMIT,
                            EventEmitContext(
                                session=self,
                                run_id=self.active_run_id or run_id,
                                turn_index=None,
                                deps=getattr(self, "run_deps", None),
                                event=event,
                            ),
                        )
            finally:
                self._active = False
                self.active_run_id = None

        return iterator()

    def abort(self) -> None:
        import asyncio

        self._abort_controller.abort()
        # Cancel any running background worker tasks so they don't write into
        # a dead session after the run ends.
        for handle in self.workers.values():
            task = getattr(handle, "task", None)
            if task is not None and isinstance(task, asyncio.Task) and not task.done():
                task.cancel()

    def mark_compaction_used(self) -> None:
        self.compaction_retry_used_this_turn = True

    async def append(self, messages: list[Message]) -> None:
        await self.store.append_messages(self.id, messages)
        self.provider_view.extend(messages)
        self.full_history.extend(messages)

    async def update_meta(self, patch: dict[str, object]) -> None:
        updated = await self.store.update_meta(self.id, patch)
        self.meta.update(updated.meta)
