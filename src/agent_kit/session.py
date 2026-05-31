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
    for this run when set.  See :class:`~agent_kit.types.OutputSchema`."""
    tool_choice: Any = None  # ToolChoice | None
    """Tool-choice override for this run.  See ``agent_kit.types.ToolChoice``."""
    final_tool_name: str | None = None
    """Name of a tool whose invocation terminates the loop and sets
    ``ResultEvent.structured_output`` from the tool-use input.  Overrides
    ``Agent.final_tool_name``."""
    deps: Any = None
    """Per-run dependency object passed into :attr:`ToolContext.deps`.
    Overrides ``Agent.deps`` when set to a non-``None`` value."""


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
    compaction_retry_used_this_turn: bool = False
    pending_skill_overlay: SkillOverlay | None = None
    current_turn_allowed_tools: list[str] | None = None
    invoked_skills: list[InvokedSkillRecord] = field(default_factory=list)
    skills_loaded_emitted: bool = False
    tools_override: ToolRegistry | None = None
    system_blocks_override: Any = None
    file_read_tracker: FileReadTracker = field(default_factory=FileReadTracker)
    _abort_controller: AbortContext = field(default_factory=AbortContext)
    run_deps: Any = None
    """Resolved dependency object for the current run.  Set by ``run_loop``
    from ``RunOptions.deps`` (falling back to ``Agent.deps``) and threaded
    into :attr:`~agent_kit.tools.ToolContext.deps` via the scheduler."""

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
            from .loop import run_loop

            try:
                async for event in run_loop(self, prompt, opts or RunOptions()):
                    yield event
            finally:
                self._active = False
                self.active_run_id = None

        return iterator()

    def abort(self) -> None:
        self._abort_controller.abort()

    def mark_compaction_used(self) -> None:
        self.compaction_retry_used_this_turn = True

    async def append(self, messages: list[Message]) -> None:
        await self.store.append_messages(self.id, messages)
        self.provider_view.extend(messages)
        self.full_history.extend(messages)

    async def update_meta(self, patch: dict[str, object]) -> None:
        updated = await self.store.update_meta(self.id, patch)
        self.meta.update(updated.meta)
