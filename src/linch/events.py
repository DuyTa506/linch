from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias, cast

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
    cost_usd: float | None = None
    """USD cost for the current turn, or ``None`` for unknown models."""
    cumulative_cost_usd: float | None = None
    """Accumulated USD cost across all turns so far, or ``None`` if no priced
    turn has run yet."""


@dataclass(slots=True)
class BudgetEvent:
    """Emitted when a :class:`~linch.budget.RunBudget` crosses its warning
    ratio (``kind="warning"``, once per budget object) or is exhausted
    (``kind="exceeded"``, after which the run stops with an error result)."""

    kind: Literal["warning", "exceeded"]
    spent_tokens: int
    spent_usd: float
    max_tokens: int | None
    max_cost_usd: float | None
    type: Literal["budget"] = "budget"


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
    total_cost_usd: float | None = None
    """Total USD cost across all turns, or ``None`` if the model is not in the
    pricing table.  Partial sums are possible for multi-model runs where only
    some turns have known pricing."""
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
class ModelFallbackEvent:
    """Emitted when the active model is swapped after a provider overload.

    The swap is run-level: every subsequent turn uses ``to_model`` until the
    run ends or another overload escalates to the next fallback.
    """

    from_model: str
    to_model: str
    reason: str = ""
    type: Literal["model_fallback"] = "model_fallback"


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
class BackgroundWorkerEvent:
    """Emitted when a background subagent worker is spawned or completes."""

    worker_id: str
    status: str  # "started" | "completed" | "failed" | "aborted" | "killed"
    display_name: str
    type: Literal["background_worker"] = "background_worker"


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


@dataclass(slots=True)
class VerificationEvent:
    """Emitted when a closed-loop gate acts on a would-be-final answer.

    Attributes:
        verifier: Name of the gate — a custom verifier's ``name`` or
            ``"output_schema"`` for the built-in structured-output repair.
        action: ``"retry"`` (feedback injected, loop continues),
            ``"stop"`` (run fails), or ``"exhausted"`` (a retry verdict was
            returned but no retries remain; the answer is accepted as-is).
        feedback: The feedback or error message attached to the verdict.
        attempt: Retry attempts used so far in this run (1-based on the
            first retry; shared across all verifiers in the same run).
    """

    verifier: str
    action: str
    feedback: str = ""
    attempt: int = 0
    type: Literal["verification"] = "verification"


@dataclass(slots=True)
class HookEventRecord:
    event: str
    hook: str
    action: str
    reason: str = ""
    type: Literal["hook"] = "hook"


@dataclass(slots=True)
class ScheduleEvent:
    """Emitted when a :class:`~linch.Schedule` fires.

    The fired payload is also enqueued into ``session.pending_notifications`` and
    surfaces as a ``UserEvent`` on the next turn (the same drain background
    workers use). This event is the observability signal for the firing itself.
    """

    schedule_id: str
    status: str  # "fired"
    payload: str = ""
    type: Literal["schedule"] = "schedule"


@dataclass(slots=True)
class WorkflowEvent:
    """Progress/journal event emitted by the workflow engine.

    ``kind="agent_end"`` and ``kind="agent_replayed"`` records double as the
    resume journal: persisted to the run store, they let an unchanged
    ``wf.agent`` call prefix replay cached results on resume.
    """

    kind: Literal["phase", "agent_start", "agent_end", "agent_replayed"]
    title: str = ""
    call_key: str = ""
    occurrence: int = 0
    subagent_type: str = ""
    result_text: str | None = None
    type: Literal["workflow"] = "workflow"


Event: TypeAlias = (
    SystemEvent
    | UserEvent
    | AssistantEvent
    | PartialAssistantEvent
    | ToolCallStartEvent
    | ToolCallEndEvent
    | PermissionRequestEvent
    | UsageEvent
    | BudgetEvent
    | CompactionEvent
    | ModelFallbackEvent
    | ContextBuildEvent
    | ResultEvent
    | ErrorEvent
    | SkillsLoadedEvent
    | SkillInvokedEvent
    | SkillCompletedEvent
    | SubagentEvent
    | BackgroundWorkerEvent
    | LoopGuardEvent
    | VerificationEvent
    | HookEventRecord
    | ScheduleEvent
    | WorkflowEvent
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


def is_budget_event(e: Event) -> bool:
    return e.type == "budget"  # type: ignore[comparison-overlap]


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


def is_verification_event(e: Event) -> bool:
    return e.type == "verification"  # type: ignore[comparison-overlap]


def is_hook_event(e: Event) -> bool:
    return e.type == "hook"  # type: ignore[comparison-overlap]


def is_workflow_event(e: Event) -> bool:
    return e.type == "workflow"  # type: ignore[comparison-overlap]


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
    if result.recovery_hint:
        out["recovery_hint"] = result.recovery_hint
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
        recovery_hint=str(raw.get("recovery_hint", "")),
    )


def event_to_dict(event: Event) -> dict[str, Any]:
    if isinstance(event, SystemEvent):
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
    if isinstance(event, UserEvent):
        return {"type": event.type, "message": message_to_dict(event.message)}
    if isinstance(event, AssistantEvent):
        return {
            "type": event.type,
            "message": message_to_dict(event.message),
            "stop_reason": event.stop_reason,
        }
    if isinstance(event, PartialAssistantEvent):
        return {"type": event.type, "delta": event.delta}
    if isinstance(event, ToolCallStartEvent):
        return {
            "type": event.type,
            "tool_use_id": event.tool_use_id,
            "tool_name": event.tool_name,
            "input": event.input,
            "summary": event.summary,
        }
    if isinstance(event, ToolCallEndEvent):
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
    if isinstance(event, PermissionRequestEvent):
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
    if isinstance(event, UsageEvent):
        d_usage: dict[str, Any] = {
            "type": event.type,
            "usage": usage_to_dict(event.usage),
            "cumulative": usage_to_dict(event.cumulative),
        }
        if event.cost_usd is not None:
            d_usage["cost_usd"] = event.cost_usd
        if event.cumulative_cost_usd is not None:
            d_usage["cumulative_cost_usd"] = event.cumulative_cost_usd
        return d_usage
    if isinstance(event, BudgetEvent):
        return {
            "type": event.type,
            "kind": event.kind,
            "spent_tokens": event.spent_tokens,
            "spent_usd": event.spent_usd,
            "max_tokens": event.max_tokens,
            "max_cost_usd": event.max_cost_usd,
        }
    if isinstance(event, CompactionEvent):
        return {
            "type": event.type,
            "messages_before": event.messages_before,
            "messages_after": event.messages_after,
            "tokens_before": event.tokens_before,
            "tokens_after": event.tokens_after,
            "strategy": event.strategy,
        }
    if isinstance(event, ModelFallbackEvent):
        return {
            "type": event.type,
            "from_model": event.from_model,
            "to_model": event.to_model,
            "reason": event.reason,
        }
    if isinstance(event, ScheduleEvent):
        return {
            "type": event.type,
            "schedule_id": event.schedule_id,
            "status": event.status,
            "payload": event.payload,
        }
    if isinstance(event, ContextBuildEvent):
        return {
            "type": event.type,
            "system_blocks": event.system_blocks,
            "messages": event.messages,
            "selected_tools": event.selected_tools,
            "budget": dict(event.budget),
            "metadata": dict(event.metadata),
        }
    if isinstance(event, ResultEvent):
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
        if event.total_cost_usd is not None:
            d["total_cost_usd"] = event.total_cost_usd
        return d
    if isinstance(event, ErrorEvent):
        return {"type": event.type, "error": event.error}
    if isinstance(event, SkillsLoadedEvent):
        return {"type": event.type, "skills": event.skills}
    if isinstance(event, SkillInvokedEvent):
        return {
            "type": event.type,
            "name": event.name,
            "args": event.args,
            "model_override": event.model_override,
            "allowed_tools": event.allowed_tools,
        }
    if isinstance(event, SkillCompletedEvent):
        return {"type": event.type, "name": event.name, "is_error": event.is_error}
    if isinstance(event, SubagentEvent):
        return {
            "type": event.type,
            "parent_session_id": event.parent_session_id,
            "subagent_run_id": event.subagent_run_id,
            "subagent_type": event.subagent_type,
            "display_name": event.display_name,
            "event": event_to_dict(event.event),
        }
    if isinstance(event, BackgroundWorkerEvent):
        return {
            "type": event.type,
            "worker_id": event.worker_id,
            "status": event.status,
            "display_name": event.display_name,
        }
    if isinstance(event, LoopGuardEvent):
        return {
            "type": event.type,
            "reason": event.reason,
            "detail": event.detail,
            "action": event.action,
        }
    if isinstance(event, VerificationEvent):
        return {
            "type": event.type,
            "verifier": event.verifier,
            "action": event.action,
            "feedback": event.feedback,
            "attempt": event.attempt,
        }
    if isinstance(event, HookEventRecord):
        return {
            "type": event.type,
            "event": event.event,
            "hook": event.hook,
            "action": event.action,
            "reason": event.reason,
        }
    if isinstance(event, WorkflowEvent):
        return {
            "type": event.type,
            "kind": event.kind,
            "title": event.title,
            "call_key": event.call_key,
            "occurrence": event.occurrence,
            "subagent_type": event.subagent_type,
            "result_text": event.result_text,
        }
    raise ValueError(f"unknown event type: {getattr(event, 'type', '<missing>')}")


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
        _cost = raw.get("cost_usd")
        _cum_cost = raw.get("cumulative_cost_usd")
        return UsageEvent(
            usage=usage_from_dict(dict(raw.get("usage", {}))),
            cumulative=usage_from_dict(dict(raw.get("cumulative", {}))),
            cost_usd=float(_cost) if isinstance(_cost, (int, float)) else None,
            cumulative_cost_usd=float(_cum_cost) if isinstance(_cum_cost, (int, float)) else None,
        )
    if typ == "budget":
        _max_tokens = raw.get("max_tokens")
        _max_cost = raw.get("max_cost_usd")
        return BudgetEvent(
            kind="exceeded" if raw.get("kind") == "exceeded" else "warning",
            spent_tokens=int(raw.get("spent_tokens", 0) or 0),
            spent_usd=float(raw.get("spent_usd", 0.0) or 0.0),
            max_tokens=int(_max_tokens) if isinstance(_max_tokens, int) else None,
            max_cost_usd=float(_max_cost) if isinstance(_max_cost, (int, float)) else None,
        )
    if typ == "compaction":
        return CompactionEvent(
            messages_before=int(raw.get("messages_before", 0) or 0),
            messages_after=int(raw.get("messages_after", 0) or 0),
            tokens_before=int(raw.get("tokens_before", 0) or 0),
            tokens_after=int(raw.get("tokens_after", 0) or 0),
            strategy=str(raw.get("strategy", "")),
        )
    if typ == "model_fallback":
        return ModelFallbackEvent(
            from_model=str(raw.get("from_model", "")),
            to_model=str(raw.get("to_model", "")),
            reason=str(raw.get("reason", "")),
        )
    if typ == "schedule":
        return ScheduleEvent(
            schedule_id=str(raw.get("schedule_id", "")),
            status=str(raw.get("status", "")),
            payload=str(raw.get("payload", "")),
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
        _total_cost = raw.get("total_cost_usd")
        return ResultEvent(
            subtype=raw.get("subtype", "error"),
            stop_reason=raw.get("stop_reason", "error"),
            total_usage=usage_from_dict(dict(raw.get("total_usage", {}))),
            duration_ms=int(raw.get("duration_ms", 0) or 0),
            final_text=raw.get("final_text") if isinstance(raw.get("final_text"), str) else None,
            structured_output=dict(so_raw) if isinstance(so_raw, dict) else None,
            structured_error=str(se_raw) if isinstance(se_raw, str) else None,
            total_cost_usd=float(_total_cost) if isinstance(_total_cost, (int, float)) else None,
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
    if typ == "background_worker":
        return BackgroundWorkerEvent(
            worker_id=str(raw.get("worker_id", "")),
            status=str(raw.get("status", "")),
            display_name=str(raw.get("display_name", "")),
        )
    if typ == "loop_guard":
        return LoopGuardEvent(
            reason=str(raw.get("reason", "")),
            detail=str(raw.get("detail", "")),
            action=str(raw.get("action", "stop")),
        )
    if typ == "verification":
        return VerificationEvent(
            verifier=str(raw.get("verifier", "")),
            action=str(raw.get("action", "")),
            feedback=str(raw.get("feedback", "")),
            attempt=int(raw.get("attempt", 0) or 0),
        )
    if typ == "hook":
        return HookEventRecord(
            event=str(raw.get("event", "")),
            hook=str(raw.get("hook", "")),
            action=str(raw.get("action", "")),
            reason=str(raw.get("reason", "")),
        )
    if typ == "workflow":
        _kind = raw.get("kind")
        if _kind not in ("phase", "agent_start", "agent_end", "agent_replayed"):
            _kind = "phase"
        return WorkflowEvent(
            kind=cast(Any, _kind),
            title=str(raw.get("title", "")),
            call_key=str(raw.get("call_key", "")),
            occurrence=int(raw.get("occurrence", 0) or 0),
            subagent_type=str(raw.get("subagent_type", "")),
            result_text=(
                raw.get("result_text") if isinstance(raw.get("result_text"), str) else None
            ),
        )
    raise ValueError(f"unknown event type: {typ!r}")
