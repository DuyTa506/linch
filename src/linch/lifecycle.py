"""Typed lifecycle points shared by kernel producers and consumers."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any, Literal

DispatchMode = Literal["emit", "serial", "waterfall"]


@dataclass(frozen=True, slots=True)
class LifecyclePoint:
    """Name one lifecycle point and its payload and dispatch contracts."""

    name: str
    _payload_ref: str | tuple[str, ...]
    mode: DispatchMode

    @property
    def payload_type(self) -> type[Any] | tuple[type[Any], ...]:
        """Resolve and return the payload type(s) for this point."""
        refs = self._payload_ref if isinstance(self._payload_ref, tuple) else (self._payload_ref,)
        resolved = tuple(_resolve_type(ref) for ref in refs)
        return resolved[0] if len(resolved) == 1 else resolved


def _resolve_type(ref: str) -> type[Any]:
    module_name, _, qualname = ref.rpartition(".")
    value: Any = importlib.import_module(module_name)
    for part in qualname.split("."):
        value = getattr(value, part)
    return value


def _hook(name: str, payload: str) -> LifecyclePoint:
    return LifecyclePoint(name, "linch.hooks.contexts." + payload, "serial")


AGENT_START = _hook("AgentStart", "AgentStartContext")
AGENT_STOP = _hook("AgentStop", "AgentStopContext")
USER_PROMPT_SUBMIT = _hook("UserPromptSubmit", "UserPromptSubmitContext")
TURN_START = _hook("TurnStart", "TurnStartContext")
TURN_STOP = _hook("TurnStop", "TurnStopContext")
BEFORE_PROVIDER_CALL = _hook("BeforeProviderCall", "BeforeProviderCallContext")
PROVIDER_CALL_START = _hook("ProviderCallStart", "ProviderCallStartContext")
PROVIDER_CALL_STOP = _hook("ProviderCallStop", "ProviderCallStopContext")
AFTER_PROVIDER_CALL = _hook("AfterProviderCall", "AfterProviderCallContext")
PRE_TOOL_USE = _hook("PreToolUse", "PreToolUseContext")
TOOL_USE_START = _hook("ToolUseStart", "ToolUseStartContext")
TOOL_USE_STOP = _hook("ToolUseStop", "ToolUseStopContext")
POST_TOOL_USE = _hook("PostToolUse", "PostToolUseContext")
POST_TOOL_USE_FAILURE = _hook("PostToolUseFailure", "PostToolUseFailureContext")
PRE_COMPACT = _hook("PreCompact", "PreCompactContext")
POST_COMPACT = _hook("PostCompact", "PostCompactContext")
BEFORE_FINAL_ANSWER = _hook("BeforeFinalAnswer", "BeforeFinalAnswerContext")
STOP = _hook("Stop", "StopContext")
SUBAGENT_START = _hook("SubagentStart", "SubagentStartContext")
SUBAGENT_STOP = _hook("SubagentStop", "SubagentStopContext")
EVENT_EMIT = _hook("EventEmit", "EventEmitContext")

TOOL_PRE_EXECUTE = LifecyclePoint(
    "tools/pre-execute", "linch.tools.pipeline.ToolExecution", "waterfall"
)
TOOL_EXECUTE = LifecyclePoint("tools/execute", "linch.tools.pipeline.ToolExecution", "waterfall")
TOOL_POST_EXECUTE = LifecyclePoint(
    "tools/post-execute",
    ("linch.tools.pipeline.ToolExecution", "builtins.object"),
    "waterfall",
)
TURN_STOPPING = LifecyclePoint("turn-stopping", "linch.loop.turn_stopping.TurnStopping", "serial")
CONTEXT_SERVICE = LifecyclePoint(
    "context/service", ("linch.kernel.context.Context", "builtins.str"), "emit"
)

LIFECYCLE_POINTS = tuple(value for value in globals().values() if isinstance(value, LifecyclePoint))
