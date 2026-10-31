from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

from .. import lifecycle
from ..tools import CanonicalToolOutput, ToolResult
from ..types import AssistantAssembly, ProviderRequest


class HookEvent(StrEnum):
    """Name a lifecycle event accepted by the hook dispatcher.

    Enum members use canonical lifecycle-point names so hooks can register
    without depending on string literals.

    Args:
        value: Lifecycle event name used to look up a member. Enum-factory-only
            construction arguments are unsupported for this populated enum.
    """

    AGENT_START = lifecycle.AGENT_START.name
    AGENT_STOP = lifecycle.AGENT_STOP.name
    USER_PROMPT_SUBMIT = lifecycle.USER_PROMPT_SUBMIT.name
    TURN_START = lifecycle.TURN_START.name
    TURN_STOP = lifecycle.TURN_STOP.name
    BEFORE_PROVIDER_CALL = lifecycle.BEFORE_PROVIDER_CALL.name
    PROVIDER_CALL_START = lifecycle.PROVIDER_CALL_START.name
    PROVIDER_CALL_STOP = lifecycle.PROVIDER_CALL_STOP.name
    AFTER_PROVIDER_CALL = lifecycle.AFTER_PROVIDER_CALL.name
    PRE_TOOL_USE = lifecycle.PRE_TOOL_USE.name
    TOOL_USE_START = lifecycle.TOOL_USE_START.name
    TOOL_USE_STOP = lifecycle.TOOL_USE_STOP.name
    POST_TOOL_USE = lifecycle.POST_TOOL_USE.name
    POST_TOOL_USE_FAILURE = lifecycle.POST_TOOL_USE_FAILURE.name
    PRE_COMPACT = lifecycle.PRE_COMPACT.name
    POST_COMPACT = lifecycle.POST_COMPACT.name
    BEFORE_FINAL_ANSWER = lifecycle.BEFORE_FINAL_ANSWER.name
    STOP = lifecycle.STOP.name
    SUBAGENT_START = lifecycle.SUBAGENT_START.name
    SUBAGENT_STOP = lifecycle.SUBAGENT_STOP.name
    EVENT_EMIT = lifecycle.EVENT_EMIT.name


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

    Attributes:
        checkpoint_key: Stable extension-owned key used to store hook state.
    """

    checkpoint_key: str

    def checkpoint_state(self, session: Any, run_id: str) -> dict[str, Any]:
        """Capture JSON-safe state for a durable checkpoint.

        Args:
            session: Active session whose state is being checkpointed.
            run_id: Run identifier being checkpointed.

        Returns:
            JSON-object state owned by this hook.
        """

        ...

    def restore_checkpoint_state(self, state: dict[str, Any], session: Any, run_id: str) -> None:
        """Restore hook state from a durable checkpoint.

        Args:
            state: JSON-object state previously returned by this hook.
            session: Active session being resumed.
            run_id: Run identifier being resumed.
        """

        ...


@dataclass(slots=True)
class HookResult:
    """Represent a hook decision and optional context mutations.

    Attributes:
        action: Dispatch decision consumed by the lifecycle runner.
        metadata: Extra values such as emitted events or host-specific data.

    Args:
        action: Decision to continue, block, mutate, retry, stop, or resolve.
        reason: Machine-readable explanation for the decision.
        feedback: Feedback text attached to a retry or block.
        prompt: Replacement prompt text, when a hook mutates it.
        images: Replacement prompt images, when supplied.
        request: Replacement provider request, when supplied.
        assembly: Replacement assistant assembly, when supplied.
        input: Replacement tool input, when supplied.
        tool_result: Replacement legacy tool result, when supplied.
        tool_output: Replacement canonical tool output, when supplied.
        final_text: Replacement final text, when supplied.
        structured_output: Replacement structured output, when supplied.
        structured_error: Replacement structured-output error, when supplied.
        result_event: Replacement terminal event, when supplied.
        metadata: Additional dispatch metadata.
    """

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
        """Return a no-op result that allows dispatch to continue.

        Returns:
            A continuing hook result.
        """

        return cls(action="continue")

    @classmethod
    def block(cls, reason: str, *, feedback: str = "", **kwargs: Any) -> HookResult:
        """Create a result that blocks the current operation.

        Args:
            reason: Explanation recorded for the block.
            feedback: Optional feedback exposed to the caller.
            **kwargs: Additional :class:`HookResult` fields to preserve.

        Returns:
            A blocking hook result.
        """

        return cls(action="block", reason=reason, feedback=feedback, **kwargs)

    @classmethod
    def mutate(cls, **kwargs: Any) -> HookResult:
        """Create a result that applies supplied context mutations.

        Args:
            **kwargs: :class:`HookResult` fields containing replacement values.

        Returns:
            A mutating hook result.
        """

        return cls(action="mutate", **kwargs)

    @classmethod
    def retry(cls, feedback: str, *, reason: str = "", **kwargs: Any) -> HookResult:
        """Create a result that requests another turn.

        Args:
            feedback: Guidance appended before the retry.
            reason: Optional machine-readable retry reason.
            **kwargs: Additional :class:`HookResult` fields to preserve.

        Returns:
            A retrying hook result.
        """

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
        """Create a result that stops the current run.

        Args:
            reason: Optional machine-readable stop reason.
            feedback: Optional feedback attached to the stop.
            result_event: Optional terminal event to return.
            **kwargs: Additional :class:`HookResult` fields to preserve.

        Returns:
            A stopping hook result.
        """

        return cls(
            action="stop",
            reason=reason,
            feedback=feedback,
            result_event=result_event,
            **kwargs,
        )

    @classmethod
    def force_continue(cls, feedback: str, *, reason: str = "", **kwargs: Any) -> HookResult:
        """Create a result that forces continuation after a stop candidate.

        Args:
            feedback: Guidance for the forced continuation.
            reason: Optional machine-readable reason.
            **kwargs: Additional :class:`HookResult` fields to preserve.

        Returns:
            A force-continuation result.
        """

        return cls(action="force_continue", feedback=feedback, reason=reason, **kwargs)

    @classmethod
    def resolve(
        cls,
        *,
        tool_result: ToolResult | None = None,
        tool_output: CanonicalToolOutput | None = None,
        **kwargs: Any,
    ) -> HookResult:
        """Short-circuit a tool call at ``PreToolUse`` with a supplied result.

        Args:
            tool_result: Legacy result to use instead of executing the tool.
            tool_output: Canonical output to use instead of executing the tool.
            **kwargs: Additional :class:`HookResult` fields to preserve.

        Returns:
            A resolving hook result.
        """
        return cls(action="resolve", tool_result=tool_result, tool_output=tool_output, **kwargs)

    def with_events(self, events: list[Any]) -> HookResult:
        """Append telemetry events to this result in place.

        Args:
            events: Events to append after any existing metadata events.

        Returns:
            This hook result, allowing fluent composition.
        """

        self.metadata = {**self.metadata, "events": [*self.metadata.get("events", []), *events]}
        return self
