from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..context import ContextBuildResult
from ..tools import ToolResult
from ..types import AssistantAssembly, ProviderRequest, StopReason, ToolUseBlock


@dataclass(slots=True)
class HookContext:
    session: Any
    run_id: str
    turn_index: int | None
    deps: Any = None


@dataclass(slots=True)
class UserPromptSubmitContext(HookContext):
    prompt: str = ""
    images: list[dict[str, str]] | None = None


@dataclass(slots=True)
class AgentStartContext(HookContext):
    model: str = ""
    prompt: str = ""
    tools: tuple[str, ...] = ()


@dataclass(slots=True)
class AgentStopContext(HookContext):
    result: Any = None


@dataclass(slots=True)
class TurnStartContext(HookContext):
    pass


@dataclass(slots=True)
class TurnStopContext(HookContext):
    pass


@dataclass(slots=True)
class BeforeProviderCallContext(HookContext):
    request: ProviderRequest | None = None
    context_result: ContextBuildResult | None = None


@dataclass(slots=True)
class ProviderCallStartContext(HookContext):
    model: str = ""


@dataclass(slots=True)
class ProviderCallStopContext(HookContext):
    model: str = ""
    stop_reason: str = ""
    usage: Any = None
    duration_ms: int = 0


@dataclass(slots=True)
class AfterProviderCallContext(HookContext):
    assembly: AssistantAssembly | None = None


@dataclass(slots=True)
class PreToolUseContext(HookContext):
    tool_use_id: str = ""
    tool_name: str = ""
    input: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    tool: Any = None


@dataclass(slots=True)
class ToolUseStartContext(HookContext):
    tool_use_id: str = ""
    tool_name: str = ""
    input: dict[str, Any] = field(default_factory=dict)
    summary: str = ""


@dataclass(slots=True)
class ToolUseStopContext(HookContext):
    tool_use_id: str = ""
    tool_name: str = ""
    is_error: bool = False
    duration_ms: int = 0
    result: str = ""
    tool_result: ToolResult | None = None


@dataclass(slots=True)
class PostToolUseContext(HookContext):
    tool_use_id: str = ""
    tool_name: str = ""
    input: dict[str, Any] = field(default_factory=dict)
    result: ToolResult | None = None


@dataclass(slots=True)
class PostToolUseFailureContext(HookContext):
    tool_use_id: str = ""
    tool_name: str = ""
    input: dict[str, Any] = field(default_factory=dict)
    result: ToolResult | None = None


@dataclass(slots=True)
class PreCompactContext(HookContext):
    messages: int = 0
    tokens: int = 0
    strategy: str = ""


@dataclass(slots=True)
class PostCompactContext(HookContext):
    messages_before: int = 0
    messages_after: int = 0
    tokens_before: int = 0
    tokens_after: int = 0
    strategy: str = ""


@dataclass(slots=True)
class BeforeFinalAnswerContext(HookContext):
    final_text: str | None = None
    structured_output: dict[str, Any] | None = None
    structured_error: str | None = None
    stop_reason: StopReason = "end_turn"
    final_tool_name: str | None = None
    tool_use: ToolUseBlock | None = None


@dataclass(slots=True)
class StopContext(HookContext):
    result_event: Any = None


@dataclass(slots=True)
class SubagentStartContext(HookContext):
    child_session_id: str = ""
    subagent_run_id: str = ""
    subagent_type: str = ""
    display_name: str = ""
    prompt: str = ""


@dataclass(slots=True)
class SubagentStopContext(HookContext):
    child_session_id: str = ""
    subagent_run_id: str = ""
    subagent_type: str = ""
    display_name: str = ""
    result: Any = None


@dataclass(slots=True)
class EventEmitContext(HookContext):
    event: Any = None
