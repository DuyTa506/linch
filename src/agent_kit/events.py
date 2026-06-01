from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias

from .tools.base import Citation, ToolResult
from .types import Message, StopReason, Usage, message_from_dict, message_to_dict


@dataclass(slots=True)
class SystemEvent:
    session_id: str
    run_id: str
    model: str
    tools: list[str]
    permission_mode: str
    cwd: str
    type: Literal["system"] = "system"
    subtype: Literal["init"] = "init"


@dataclass(slots=True)
class UserEvent:
    message: Message
    type: Literal["user"] = "user"


@dataclass(slots=True)
class AssistantEvent:
    message: Message
    stop_reason: StopReason
    type: Literal["assistant"] = "assistant"


@dataclass(slots=True)
class PartialAssistantEvent:
    delta: dict[str, Any]
    type: Literal["partial_assistant"] = "partial_assistant"


@dataclass(slots=True)
class ToolCallStartEvent:
    tool_use_id: str
    tool_name: str
    input: dict[str, Any]
    summary: str
    type: Literal["tool_call_start"] = "tool_call_start"


@dataclass(slots=True)
class ToolCallEndEvent:
    tool_use_id: str
    tool_name: str
    result: str = ""
    is_error: bool = False
    duration_ms: int = 0
    tool_result: ToolResult | None = None
    type: Literal["tool_call_end"] = "tool_call_end"


@dataclass(slots=True)
class PermissionRequestItem:
    tool_use_id: str
    tool_name: str
    input: dict[str, Any]
    summary: str


@dataclass(slots=True)
class PermissionRequestEvent:
    requests: list[PermissionRequestItem]
    type: Literal["permission_request"] = "permission_request"


@dataclass(slots=True)
class UsageEvent:
    usage: Usage
    cumulative: Usage
    type: Literal["usage"] = "usage"


@dataclass(slots=True)
class ResultEvent:
    subtype: Literal["success", "error", "aborted"]
    stop_reason: StopReason
    total_usage: Usage
    duration_ms: int
    final_text: str | None = None
    structured_output: dict[str, Any] | None = None
    """Parsed JSON output when an ``OutputSchema`` was configured.  ``None``
    when no schema was set or when parsing failed (check ``structured_error``
    for the failure reason)."""
    structured_error: str | None = None
    """Error message from JSON parsing / schema validation.  Set when
    ``output_schema`` was configured but the model's response was not valid
    JSON or did not match the schema."""
    type: Literal["result"] = "result"


@dataclass(slots=True)
class ErrorEvent:
    error: dict[str, Any]
    type: Literal["error"] = "error"


@dataclass(slots=True)
class CompactionEvent:
    messages_before: int
    messages_after: int
    tokens_before: int
    tokens_after: int
    strategy: str
    type: Literal["compaction"] = "compaction"


@dataclass(slots=True)
class ContextBuildEvent:
    system_blocks: int
    messages: int
    selected_tools: list[str] | None
    budget: dict[str, Any]
    metadata: dict[str, Any]
    type: Literal["context_build"] = "context_build"


@dataclass(slots=True)
class SkillsLoadedEvent:
    skills: list[dict[str, Any]]
    type: Literal["skills_loaded"] = "skills_loaded"


@dataclass(slots=True)
class SkillInvokedEvent:
    name: str
    args: str | None = None
    model_override: str | None = None
    allowed_tools: list[str] | None = None
    type: Literal["skill_invoked"] = "skill_invoked"


@dataclass(slots=True)
class SkillCompletedEvent:
    name: str
    is_error: bool = False
    type: Literal["skill_completed"] = "skill_completed"


@dataclass(slots=True)
class SubagentEvent:
    parent_session_id: str
    subagent_run_id: str
    subagent_type: str
    display_name: str
    event: Event
    type: Literal["subagent_event"] = "subagent_event"


@dataclass(slots=True)
class LoopGuardEvent:
    """Emitted when the loop guard trips or when ``max_turns`` is reached.

    Attributes:
        reason: Machine-readable tag for the trip condition.  One of
            ``"repeated_tool_call"``, ``"repeated_failures"``, or
            ``"max_turns"``.
        detail: Human-readable description of why the guard tripped.
        action: What the loop did in response — ``"stop"`` (hard error
            termination) or ``"force_final"`` (one tools-disabled turn
            injected before stopping).
    """

    reason: str
    detail: str
    action: str
    type: Literal["loop_guard"] = "loop_guard"


Event: TypeAlias = (
    SystemEvent
    | UserEvent
    | AssistantEvent
    | PartialAssistantEvent
    | ToolCallStartEvent
    | ToolCallEndEvent
    | PermissionRequestEvent
    | UsageEvent
    | CompactionEvent
    | ContextBuildEvent
    | ResultEvent
    | ErrorEvent
    | SkillsLoadedEvent
    | SkillInvokedEvent
    | SkillCompletedEvent
    | SubagentEvent
    | LoopGuardEvent
)


def is_system_event(e: Event) -> bool:
    return e.type == "system"  # type: ignore[comparison-overlap]


def is_assistant_event(e: Event) -> bool:
    return e.type == "assistant"  # type: ignore[comparison-overlap]


def is_user_event(e: Event) -> bool:
    return e.type == "user"  # type: ignore[comparison-overlap]


def is_partial_assistant_event(e: Event) -> bool:
    return e.type == "partial_assistant"  # type: ignore[comparison-overlap]


def is_tool_call_start_event(e: Event) -> bool:
    return e.type == "tool_call_start"  # type: ignore[comparison-overlap]


def is_tool_call_end_event(e: Event) -> bool:
    return e.type == "tool_call_end"  # type: ignore[comparison-overlap]


def is_permission_request_event(e: Event) -> bool:
    return e.type == "permission_request"  # type: ignore[comparison-overlap]


def is_usage_event(e: Event) -> bool:
    return e.type == "usage"  # type: ignore[comparison-overlap]


def is_compaction_event(e: Event) -> bool:
    return e.type == "compaction"  # type: ignore[comparison-overlap]


def is_context_build_event(e: Event) -> bool:
    return e.type == "context_build"  # type: ignore[comparison-overlap]


def is_result_event(e: Event) -> bool:
    return e.type == "result"  # type: ignore[comparison-overlap]


def is_error_event(e: Event) -> bool:
    return e.type == "error"  # type: ignore[comparison-overlap]


def is_skills_loaded_event(e: Event) -> bool:
    return e.type == "skills_loaded"  # type: ignore[comparison-overlap]


def is_skill_invoked_event(e: Event) -> bool:
    return e.type == "skill_invoked"  # type: ignore[comparison-overlap]


def is_skill_completed_event(e: Event) -> bool:
    return e.type == "skill_completed"  # type: ignore[comparison-overlap]


def is_subagent_event(e: Event) -> bool:
    return e.type == "subagent_event"  # type: ignore[comparison-overlap]


def is_loop_guard_event(e: Event) -> bool:
    return e.type == "loop_guard"  # type: ignore[comparison-overlap]


def usage_to_dict(usage: Usage) -> dict[str, int]:
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_creation_tokens": usage.cache_creation_tokens,
    }


def usage_from_dict(raw: dict[str, Any]) -> Usage:
    return Usage(
        input_tokens=int(raw.get("input_tokens", 0) or 0),
        output_tokens=int(raw.get("output_tokens", 0) or 0),
        cache_read_tokens=int(raw.get("cache_read_tokens", 0) or 0),
        cache_creation_tokens=int(raw.get("cache_creation_tokens", 0) or 0),
    )


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    return str(value)


def _is_json_serializable(value: Any) -> bool:
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return False
    return True


def citation_to_dict(citation: Citation) -> dict[str, Any]:
    return {
        "id": citation.id,
        "source": citation.source,
        "label": citation.label,
        "chunk": citation.chunk,
        "score": citation.score,
        "metadata": _json_safe(citation.metadata),
    }


def citation_from_dict(raw: dict[str, Any]) -> Citation:
    return Citation(
        id=str(raw.get("id", "")),
        source=str(raw.get("source", "")),
        label=raw.get("label") if isinstance(raw.get("label"), str) else None,
        chunk=raw.get("chunk") if isinstance(raw.get("chunk"), str) else None,
        score=float(raw["score"]) if isinstance(raw.get("score"), int | float) else None,
        metadata=dict(raw.get("metadata", {})) if isinstance(raw.get("metadata"), dict) else {},
    )


def tool_result_to_dict(result: ToolResult) -> dict[str, Any]:
    out: dict[str, Any] = {
        "content": result.content,
        "summary": result.summary,
        "is_error": result.is_error,
        "metadata": _json_safe(result.metadata),
        "citations": [citation_to_dict(citation) for citation in result.citations],
        "duration_ms": result.duration_ms,
        "truncated": result.truncated,
    }
    if result.attachments and all(_is_json_serializable(item) for item in result.attachments):
        out["attachments"] = result.attachments
    return out


def tool_result_from_dict(raw: dict[str, Any]) -> ToolResult:
    citations = []
    for item in raw.get("citations", []):
        if isinstance(item, dict):
            citations.append(citation_from_dict(item))
    attachments = raw.get("attachments", [])
    if not isinstance(attachments, list):
        attachments = []
    return ToolResult(
        content=str(raw.get("content", "")),
        summary=str(raw.get("summary", "")),
        is_error=bool(raw.get("is_error", False)),
        metadata=dict(raw.get("metadata", {})) if isinstance(raw.get("metadata"), dict) else {},
        citations=citations,
        attachments=attachments,
        duration_ms=int(raw.get("duration_ms", 0) or 0),
        truncated=bool(raw.get("truncated", False)),
    )


def event_to_dict(event: Event) -> dict[str, Any]:
    typ = event.type
    if typ == "system":
        return {
            "type": event.type,
            "subtype": event.subtype,
            "session_id": event.session_id,
            "run_id": event.run_id,
            "model": event.model,
            "tools": list(event.tools),
            "permission_mode": event.permission_mode,
            "cwd": event.cwd,
        }
    if typ == "user":
        return {"type": event.type, "message": message_to_dict(event.message)}
    if typ == "assistant":
        return {
            "type": event.type,
            "message": message_to_dict(event.message),
            "stop_reason": event.stop_reason,
        }
    if typ == "partial_assistant":
        return {"type": event.type, "delta": event.delta}
    if typ == "tool_call_start":
        return {
            "type": event.type,
            "tool_use_id": event.tool_use_id,
            "tool_name": event.tool_name,
            "input": event.input,
            "summary": event.summary,
        }
    if typ == "tool_call_end":
        out = {
            "type": event.type,
            "tool_use_id": event.tool_use_id,
            "tool_name": event.tool_name,
            "result": event.result,
            "is_error": event.is_error,
            "duration_ms": event.duration_ms,
        }
        if event.tool_result is not None:
            out["tool_result"] = tool_result_to_dict(event.tool_result)
        return out
    if typ == "permission_request":
        return {
            "type": event.type,
            "requests": [
                {
                    "tool_use_id": req.tool_use_id,
                    "tool_name": req.tool_name,
                    "input": req.input,
                    "summary": req.summary,
                }
                for req in event.requests
            ],
        }
    if typ == "usage":
        return {
            "type": event.type,
            "usage": usage_to_dict(event.usage),
            "cumulative": usage_to_dict(event.cumulative),
        }
    if typ == "compaction":
        return {
            "type": event.type,
            "messages_before": event.messages_before,
            "messages_after": event.messages_after,
            "tokens_before": event.tokens_before,
            "tokens_after": event.tokens_after,
            "strategy": event.strategy,
        }
    if typ == "context_build":
        return {
            "type": event.type,
            "system_blocks": event.system_blocks,
            "messages": event.messages,
            "selected_tools": event.selected_tools,
            "budget": dict(event.budget),
            "metadata": dict(event.metadata),
        }
    if typ == "result":
        d: dict[str, Any] = {
            "type": event.type,
            "subtype": event.subtype,
            "stop_reason": event.stop_reason,
            "total_usage": usage_to_dict(event.total_usage),
            "duration_ms": event.duration_ms,
            "final_text": event.final_text,
        }
        if event.structured_output is not None:
            d["structured_output"] = event.structured_output
        if event.structured_error is not None:
            d["structured_error"] = event.structured_error
        return d
    if typ == "error":
        return {"type": event.type, "error": event.error}
    if typ == "skills_loaded":
        return {"type": event.type, "skills": event.skills}
    if typ == "skill_invoked":
        return {
            "type": event.type,
            "name": event.name,
            "args": event.args,
            "model_override": event.model_override,
            "allowed_tools": event.allowed_tools,
        }
    if typ == "skill_completed":
        return {"type": event.type, "name": event.name, "is_error": event.is_error}
    if typ == "subagent_event":
        return {
            "type": event.type,
            "parent_session_id": event.parent_session_id,
            "subagent_run_id": event.subagent_run_id,
            "subagent_type": event.subagent_type,
            "display_name": event.display_name,
            "event": event_to_dict(event.event),
        }
    if typ == "loop_guard":
        return {
            "type": event.type,
            "reason": event.reason,
            "detail": event.detail,
            "action": event.action,
        }
    raise ValueError(f"unknown event type: {typ}")


def event_from_dict(raw: dict[str, Any]) -> Event:
    typ = raw.get("type")
    if typ == "system":
        return SystemEvent(
            session_id=str(raw.get("session_id", "")),
            run_id=str(raw.get("run_id", "")),
            model=str(raw.get("model", "")),
            tools=[str(t) for t in raw.get("tools", [])],
            permission_mode=str(raw.get("permission_mode", "")),
            cwd=str(raw.get("cwd", "")),
            subtype="init",
        )
    if typ == "user":
        return UserEvent(message=message_from_dict(dict(raw.get("message", {}))))
    if typ == "assistant":
        return AssistantEvent(
            message=message_from_dict(dict(raw.get("message", {}))),
            stop_reason=raw.get("stop_reason", "error"),
        )
    if typ == "partial_assistant":
        return PartialAssistantEvent(delta=dict(raw.get("delta", {})))
    if typ == "tool_call_start":
        return ToolCallStartEvent(
            tool_use_id=str(raw.get("tool_use_id", "")),
            tool_name=str(raw.get("tool_name", "")),
            input=dict(raw.get("input", {})),
            summary=str(raw.get("summary", "")),
        )
    if typ == "tool_call_end":
        raw_tool_result = raw.get("tool_result")
        return ToolCallEndEvent(
            tool_use_id=str(raw.get("tool_use_id", "")),
            tool_name=str(raw.get("tool_name", "")),
            result=str(raw.get("result", "")),
            is_error=bool(raw.get("is_error", False)),
            duration_ms=int(raw.get("duration_ms", 0) or 0),
            tool_result=tool_result_from_dict(raw_tool_result)
            if isinstance(raw_tool_result, dict)
            else None,
        )
    if typ == "permission_request":
        requests = []
        for req in raw.get("requests", []):
            if not isinstance(req, dict):
                continue
            requests.append(
                PermissionRequestItem(
                    tool_use_id=str(req.get("tool_use_id", "")),
                    tool_name=str(req.get("tool_name", "")),
                    input=dict(req.get("input", {})),
                    summary=str(req.get("summary", "")),
                )
            )
        return PermissionRequestEvent(requests=requests)
    if typ == "usage":
        return UsageEvent(
            usage=usage_from_dict(dict(raw.get("usage", {}))),
            cumulative=usage_from_dict(dict(raw.get("cumulative", {}))),
        )
    if typ == "compaction":
        return CompactionEvent(
            messages_before=int(raw.get("messages_before", 0) or 0),
            messages_after=int(raw.get("messages_after", 0) or 0),
            tokens_before=int(raw.get("tokens_before", 0) or 0),
            tokens_after=int(raw.get("tokens_after", 0) or 0),
            strategy=str(raw.get("strategy", "")),
        )
    if typ == "context_build":
        selected_raw = raw.get("selected_tools")
        return ContextBuildEvent(
            system_blocks=int(raw.get("system_blocks", 0) or 0),
            messages=int(raw.get("messages", 0) or 0),
            selected_tools=(
                [str(t) for t in selected_raw] if isinstance(selected_raw, list) else None
            ),
            budget=dict(raw.get("budget", {})),
            metadata=dict(raw.get("metadata", {})),
        )
    if typ == "result":
        so_raw = raw.get("structured_output")
        se_raw = raw.get("structured_error")
        return ResultEvent(
            subtype=raw.get("subtype", "error"),
            stop_reason=raw.get("stop_reason", "error"),
            total_usage=usage_from_dict(dict(raw.get("total_usage", {}))),
            duration_ms=int(raw.get("duration_ms", 0) or 0),
            final_text=raw.get("final_text") if isinstance(raw.get("final_text"), str) else None,
            structured_output=dict(so_raw) if isinstance(so_raw, dict) else None,
            structured_error=str(se_raw) if isinstance(se_raw, str) else None,
        )
    if typ == "error":
        return ErrorEvent(error=dict(raw.get("error", {})))
    if typ == "skills_loaded":
        return SkillsLoadedEvent(skills=list(raw.get("skills", [])))
    if typ == "skill_invoked":
        return SkillInvokedEvent(
            name=str(raw.get("name", "")),
            args=raw.get("args") if isinstance(raw.get("args"), str) else None,
            model_override=raw.get("model_override")
            if isinstance(raw.get("model_override"), str)
            else None,
            allowed_tools=[str(t) for t in raw.get("allowed_tools", [])]
            if isinstance(raw.get("allowed_tools"), list)
            else None,
        )
    if typ == "skill_completed":
        return SkillCompletedEvent(
            name=str(raw.get("name", "")),
            is_error=bool(raw.get("is_error", False)),
        )
    if typ == "subagent_event":
        nested = raw.get("event")
        if not isinstance(nested, dict):
            raise ValueError("subagent_event.event must be an object")
        return SubagentEvent(
            parent_session_id=str(raw.get("parent_session_id", "")),
            subagent_run_id=str(raw.get("subagent_run_id", "")),
            subagent_type=str(raw.get("subagent_type", "")),
            display_name=str(raw.get("display_name", "")),
            event=event_from_dict(nested),
        )
    if typ == "loop_guard":
        return LoopGuardEvent(
            reason=str(raw.get("reason", "")),
            detail=str(raw.get("detail", "")),
            action=str(raw.get("action", "stop")),
        )
    raise ValueError(f"unknown event type: {typ!r}")
