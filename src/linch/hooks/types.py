from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Protocol, runtime_checkable

from ..tools import CanonicalToolOutput, ToolResult
from ..types import AssistantAssembly, ProviderRequest


class HookEvent(str, Enum):
    AGENT_START = "AgentStart"
    AGENT_STOP = "AgentStop"
    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    TURN_START = "TurnStart"
    TURN_STOP = "TurnStop"
    BEFORE_PROVIDER_CALL = "BeforeProviderCall"
    PROVIDER_CALL_START = "ProviderCallStart"
    PROVIDER_CALL_STOP = "ProviderCallStop"
    AFTER_PROVIDER_CALL = "AfterProviderCall"
    PRE_TOOL_USE = "PreToolUse"
    TOOL_USE_START = "ToolUseStart"
    TOOL_USE_STOP = "ToolUseStop"
    POST_TOOL_USE = "PostToolUse"
    POST_TOOL_USE_FAILURE = "PostToolUseFailure"
    PRE_COMPACT = "PreCompact"
    POST_COMPACT = "PostCompact"
    BEFORE_FINAL_ANSWER = "BeforeFinalAnswer"
    STOP = "Stop"
    SUBAGENT_START = "SubagentStart"
    SUBAGENT_STOP = "SubagentStop"
    EVENT_EMIT = "EventEmit"


HookAction = Literal[
    "continue",
    "block",
    "mutate",
    "retry",
    "stop",
    "force_continue",
    "resolve",
]


@runtime_checkable
class CheckpointableHook(Protocol):
    """Optional durable-state contract for an agent hook.

    The normal hook protocol remains entirely optional and event-oriented.  A
    hook that also implements this protocol has its state captured at each
    durable checkpoint and restored for a resumed run under its stable,
    extension-owned ``checkpoint_key``.  Returned state must be a JSON object
    containing only JSON-safe values; invalid state is rejected rather than
    risking a partially durable resume.

    Both methods receive the active session and run ID, so one hook instance
    can safely serve concurrent sessions without process-global run state. They
    are deliberately synchronous: checkpoint saves are already async at the
    run-store boundary, and state capture must remain a small, deterministic
    in-memory operation on that critical path.
    """

    checkpoint_key: str

    def checkpoint_state(self, session: Any, run_id: str) -> dict[str, Any]: ...

    def restore_checkpoint_state(
        self, state: dict[str, Any], session: Any, run_id: str
    ) -> None: ...


@dataclass(slots=True)
class HookResult:
    action: HookAction = "continue"
    reason: str = ""
    feedback: str = ""
    prompt: str | None = None
    images: list[dict[str, str]] | None = None
    request: ProviderRequest | None = None
    assembly: AssistantAssembly | None = None
    input: dict[str, Any] | None = None
    tool_result: ToolResult | None = None
    tool_output: CanonicalToolOutput | None = None
    final_text: str | None = None
    structured_output: dict[str, Any] | None = None
    structured_error: str | None = None
    result_event: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def continue_(cls) -> HookResult:
        return cls(action="continue")

    @classmethod
    def block(cls, reason: str, *, feedback: str = "", **kwargs: Any) -> HookResult:
        return cls(action="block", reason=reason, feedback=feedback, **kwargs)

    @classmethod
    def mutate(cls, **kwargs: Any) -> HookResult:
        return cls(action="mutate", **kwargs)

    @classmethod
    def retry(cls, feedback: str, *, reason: str = "", **kwargs: Any) -> HookResult:
        return cls(action="retry", feedback=feedback, reason=reason, **kwargs)

    @classmethod
    def stop(
        cls,
        reason: str = "",
        *,
        feedback: str = "",
        result_event: Any = None,
        **kwargs: Any,
    ) -> HookResult:
        return cls(
            action="stop",
            reason=reason,
            feedback=feedback,
            result_event=result_event,
            **kwargs,
        )

    @classmethod
    def force_continue(cls, feedback: str, *, reason: str = "", **kwargs: Any) -> HookResult:
        return cls(action="force_continue", feedback=feedback, reason=reason, **kwargs)

    @classmethod
    def resolve(
        cls,
        *,
        tool_result: ToolResult | None = None,
        tool_output: CanonicalToolOutput | None = None,
        **kwargs: Any,
    ) -> HookResult:
        """Short-circuit a tool call at ``PreToolUse``: skip execution and use
        the supplied legacy or canonical output as its outcome."""
        return cls(action="resolve", tool_result=tool_result, tool_output=tool_output, **kwargs)

    def with_events(self, events: list[Any]) -> HookResult:
        self.metadata = {**self.metadata, "events": [*self.metadata.get("events", []), *events]}
        return self
