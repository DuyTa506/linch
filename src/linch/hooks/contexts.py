from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from ..context import ContextBuildResult
from ..tools import CanonicalToolOutput, ToolResult
from ..types import AssistantAssembly, ProviderRequest, StopReason, ToolUseBlock, Usage


@dataclass(slots=True)
class HookContext:
    """Carry run identity and dependencies to a lifecycle hook.

    Attributes:
        session: Session object associated with the lifecycle event.
        deps: Run-scoped dependency object made available to the hook.

    Args:
        session: Session associated with the lifecycle event.
        run_id: Run identifier associated with the event.
        turn_index: Current turn number, when known.
        deps: Run-scoped dependency object.
    """

    session: Any
    run_id: str
    turn_index: int | None
    deps: Any = None


@dataclass(slots=True)
class UserPromptSubmitContext(HookContext):
    """Describe a user prompt submitted to the agent.

    Args:
        session: Session receiving the prompt.
        run_id: Run identifier receiving the prompt.
        turn_index: Current turn number, when known.
        deps: Run-scoped dependency object.
        prompt: Prompt text submitted by the user.
        images: Optional image descriptors attached to the prompt.
        source: Whether the prompt came from a run or alignment action.
    """

    prompt: str = ""
    images: list[dict[str, str]] | None = None
    source: Literal["run", "align"] = "run"


@dataclass(slots=True)
class AgentStartContext(HookContext):
    """Describe agent initialization for a hook.

    Args:
        session: Session starting the run.
        run_id: Run identifier being started.
        turn_index: Current turn number, when known.
        deps: Run-scoped dependency object.
        model: Model selected for the run.
        prompt: Initial prompt text.
        tools: Names of tools available to the run.
    """

    model: str = ""
    prompt: str = ""
    tools: tuple[str, ...] = ()


@dataclass(slots=True)
class AgentStopContext(HookContext):
    """Describe agent termination for a hook.

    Args:
        session: Session whose run stopped.
        run_id: Run identifier that stopped.
        turn_index: Last turn number, when known.
        deps: Run-scoped dependency object.
        result: Terminal result or host-defined result object.
    """

    result: Any = None


@dataclass(slots=True)
class TurnStartContext(HookContext):
    """Mark the beginning of one agent turn.

    Args:
        session: Session executing the turn.
        run_id: Run identifier executing the turn.
        turn_index: Turn number being started.
        deps: Run-scoped dependency object.
    """

    pass


@dataclass(slots=True)
class TurnStopContext(HookContext):
    """Mark the end of one agent turn.

    Args:
        session: Session that executed the turn.
        run_id: Run identifier that executed the turn.
        turn_index: Turn number that stopped.
        deps: Run-scoped dependency object.
    """

    pass


@dataclass(slots=True)
class BeforeProviderCallContext(HookContext):
    """Expose a provider request before dispatch.

    Attributes:
        request: Provider request being prepared, or ``None`` on resume paths
            that have not reconstructed it yet.
        context_result: Context-builder output used to assemble the request.

    Args:
        session: Session making the provider call.
        run_id: Run identifier making the call.
        turn_index: Current turn number, when known.
        deps: Run-scoped dependency object.
        request: Provider request being prepared, when available; resume paths
            may leave it ``None``.
        context_result: Context-builder result used for the request.
    """

    request: ProviderRequest | None = None
    context_result: ContextBuildResult | None = None


@dataclass(slots=True)
class ProviderCallStartContext(HookContext):
    """Mark the start of a provider call.

    Args:
        session: Session making the provider call.
        run_id: Run identifier making the call.
        turn_index: Current turn number, when known.
        deps: Run-scoped dependency object.
        model: Model receiving the request.
    """

    model: str = ""


@dataclass(slots=True)
class ProviderCallStopContext(HookContext):
    """Describe a provider call that has stopped.

    Args:
        session: Session that made the provider call.
        run_id: Run identifier that made the call.
        turn_index: Current turn number, when known.
        deps: Run-scoped dependency object.
        model: Model that served the request.
        stop_reason: Provider stop reason.
        usage: Provider usage payload, when available.
        duration_ms: Provider-call duration in milliseconds.
    """

    model: str = ""
    stop_reason: str = ""
    usage: Any = None
    duration_ms: int = 0


@dataclass(slots=True)
class AfterProviderCallContext(HookContext):
    """Expose the assembled assistant response after a provider call.

    Attributes:
        assembly: Normalized assistant response and provider accounting.

    Args:
        session: Session that made the provider call.
        run_id: Run identifier that made the call.
        turn_index: Current turn number, when known.
        deps: Run-scoped dependency object.
        assembly: Normalized assistant assembly, when available.
    """

    assembly: AssistantAssembly | None = None


@dataclass(slots=True)
class PreToolUseContext(HookContext):
    """Expose a tool call before execution.

    Args:
        session: Session executing the tool.
        run_id: Run identifier executing the tool.
        turn_index: Current turn number, when known.
        deps: Run-scoped dependency object.
        tool_use_id: Provider identifier for the call.
        tool_name: Registered tool name.
        input: JSON-compatible tool arguments.
        summary: Human-readable call summary.
        tool: Resolved tool object, when available.
    """

    tool_use_id: str = ""
    tool_name: str = ""
    input: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    tool: Any = None


@dataclass(slots=True)
class ToolUseStartContext(HookContext):
    """Mark the start of tool execution.

    Args:
        session: Session executing the tool.
        run_id: Run identifier executing the tool.
        turn_index: Current turn number, when known.
        deps: Run-scoped dependency object.
        tool_use_id: Provider identifier for the call.
        tool_name: Registered tool name.
        input: JSON-compatible tool arguments.
        summary: Human-readable call summary.
    """

    tool_use_id: str = ""
    tool_name: str = ""
    input: dict[str, Any] = field(default_factory=dict)
    summary: str = ""


@dataclass(slots=True)
class ToolUseStopContext(HookContext):
    """Describe a completed tool execution.

    Attributes:
        result: Legacy textual result retained for compatibility.
        tool_result: Normalized legacy structured result, when available.
        tool_output: Canonical output supplied by an integration, when
            available; it is distinct from ``tool_result``.

    Args:
        session: Session that executed the tool.
        run_id: Run identifier that executed the tool.
        turn_index: Current turn number, when known.
        deps: Run-scoped dependency object.
        tool_use_id: Provider identifier for the call.
        tool_name: Registered tool name.
        is_error: Whether execution failed.
        duration_ms: Tool duration in milliseconds.
        result: Textual result retained for compatibility.
        tool_result: Normalized structured result, when available.
        tool_output: Optional canonical output supplied by an integration.
    """

    tool_use_id: str = ""
    tool_name: str = ""
    is_error: bool = False
    duration_ms: int = 0
    result: str = ""
    tool_result: ToolResult | None = None
    tool_output: CanonicalToolOutput | None = None


@dataclass(slots=True)
class PostToolUseContext(HookContext):
    """Expose a tool result after execution, including failed results.

    Attributes:
        result: Legacy normalized result, including an error result when one
            was produced.
        tool_output: Canonical output supplied by an integration, when
            available; canonical-only output bypasses legacy result adapters.

    Args:
        session: Session that executed the tool.
        run_id: Run identifier that executed the tool.
        turn_index: Current turn number, when known.
        deps: Run-scoped dependency object.
        tool_use_id: Provider identifier for the call.
        tool_name: Registered tool name.
        input: JSON-compatible tool arguments.
        result: Normalized legacy result, including errors, when available.
        tool_output: Canonical output supplied by an integration, when available.
    """

    tool_use_id: str = ""
    tool_name: str = ""
    input: dict[str, Any] = field(default_factory=dict)
    result: ToolResult | None = None
    tool_output: CanonicalToolOutput | None = None


@dataclass(slots=True)
class PostToolUseFailureContext(HookContext):
    """Expose a failed tool result after execution.

    Attributes:
        result: Legacy failed result, when available.
        tool_output: Canonical failed output, when supplied by an integration.

    Args:
        session: Session that executed the tool.
        run_id: Run identifier that executed the tool.
        turn_index: Current turn number, when known.
        deps: Run-scoped dependency object.
        tool_use_id: Provider identifier for the call.
        tool_name: Registered tool name.
        input: JSON-compatible tool arguments.
        result: Failed normalized result, when available.
        tool_output: Canonical failed output, when available.
    """

    tool_use_id: str = ""
    tool_name: str = ""
    input: dict[str, Any] = field(default_factory=dict)
    result: ToolResult | None = None
    tool_output: CanonicalToolOutput | None = None


@dataclass(slots=True)
class PreCompactContext(HookContext):
    """Describe context compaction before it runs.

    Args:
        session: Session whose context will be compacted.
        run_id: Run identifier performing compaction.
        turn_index: Current turn number, when known.
        deps: Run-scoped dependency object.
        messages: Number of messages before compaction.
        tokens: Token estimate before compaction.
        strategy: Compaction strategy selected.
    """

    messages: int = 0
    tokens: int = 0
    strategy: str = ""


@dataclass(slots=True)
class PostCompactContext(HookContext):
    """Describe context compaction after it runs.

    Args:
        session: Session whose context was compacted.
        run_id: Run identifier performing compaction.
        turn_index: Current turn number, when known.
        deps: Run-scoped dependency object.
        messages_before: Message count before compaction.
        messages_after: Message count after compaction.
        tokens_before: Token estimate before compaction.
        tokens_after: Token estimate after compaction.
        strategy: Compaction strategy used.
    """

    messages_before: int = 0
    messages_after: int = 0
    tokens_before: int = 0
    tokens_after: int = 0
    strategy: str = ""


@dataclass(slots=True)
class BeforeFinalAnswerContext(HookContext):
    """Expose a candidate final answer before it is returned.

    Attributes:
        structured_error: Structured-output parsing or validation error, when
            present.

    Args:
        session: Session producing the candidate answer.
        run_id: Run identifier producing the answer.
        turn_index: Current turn number, when known.
        deps: Run-scoped dependency object.
        final_text: Candidate textual answer.
        structured_output: Candidate structured answer, when available.
        structured_error: Structured-output error, when present.
        stop_reason: Provider reason for the candidate response.
        final_tool_name: Name of a terminal output tool, when used.
        tool_use: Terminal tool call, when used.
    """

    final_text: str | None = None
    structured_output: dict[str, Any] | None = None
    structured_error: str | None = None
    stop_reason: StopReason = "end_turn"
    final_tool_name: str | None = None
    tool_use: ToolUseBlock | None = None


@dataclass(slots=True)
class StopContext(HookContext):
    """Describe final run termination for a stop hook.

    Args:
        session: Session whose run stopped.
        run_id: Run identifier that stopped.
        turn_index: Last turn number, when known.
        deps: Run-scoped dependency object.
        result_event: Terminal event produced by the run.
    """

    result_event: Any = None


@dataclass(slots=True)
class SubagentStartContext(HookContext):
    """Describe a subagent run starting.

    Attributes:
        subagent_run_id: Child run identifier used to correlate nested events.

    Args:
        session: Parent session that spawned the subagent.
        run_id: Parent run identifier.
        turn_index: Parent turn number, when known.
        deps: Parent run-scoped dependency object.
        child_session_id: Child session identifier.
        subagent_run_id: Child run identifier.
        subagent_type: Subagent definition used by the child.
        display_name: Human-readable child name.
        prompt: Prompt sent to the child.
    """

    child_session_id: str = ""
    subagent_run_id: str = ""
    subagent_type: str = ""
    display_name: str = ""
    prompt: str = ""


@dataclass(slots=True)
class SubagentStopContext(HookContext):
    """Describe a subagent run stopping.

    Attributes:
        subagent_run_id: Child run identifier used to correlate nested events.
        result: Child terminal result, when supplied by the runner.

    Args:
        session: Parent session that spawned the subagent.
        run_id: Parent run identifier.
        turn_index: Parent turn number, when known.
        deps: Parent run-scoped dependency object.
        child_session_id: Child session identifier.
        subagent_run_id: Child run identifier.
        subagent_type: Subagent definition used by the child.
        display_name: Human-readable child name.
        result: Child terminal result.
    """

    child_session_id: str = ""
    subagent_run_id: str = ""
    subagent_type: str = ""
    display_name: str = ""
    result: Any = None


@dataclass(slots=True)
class EventEmitContext(HookContext):
    """Expose an event immediately before it is emitted.

    Args:
        session: Session emitting the event.
        run_id: Run identifier emitting the event.
        turn_index: Current turn number, when known.
        deps: Run-scoped dependency object.
        event: Event payload being emitted.
    """

    event: Any = None


@dataclass(frozen=True, slots=True)
class _AgentResult:
    """Private native lifecycle result supplied to AgentStop hooks."""

    run_id: str
    session_id: str
    subtype: str
    stop_reason: str
    total_usage: Usage
    duration_ms: int
    error: dict[str, Any] | None = None
