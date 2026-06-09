from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .events import (
    ContextBuildEvent,
    ErrorEvent,
    Event,
    LoopGuardEvent,
    PermissionRequestEvent,
    ResultEvent,
    SystemEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    UsageEvent,
    event_to_dict,
    tool_result_to_dict,
    usage_to_dict,
)
from .run_store import RunRecord, RunStore, StoredRunEvent, checkpoint_to_dict


@dataclass(slots=True)
class RunReport:
    run_id: str
    session_id: str
    status: str
    phase: str | None
    event_count: int
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    permission_requests: list[dict[str, Any]] = field(default_factory=list)
    context_builds: list[dict[str, Any]] = field(default_factory=list)
    loop_guards: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] | None = None
    final: dict[str, Any] | None = None
    checkpoint: dict[str, Any] | None = None
    long_run: dict[str, Any] = field(default_factory=dict)
    timeline: list[dict[str, Any]] = field(default_factory=list)

    @property
    def failed_tool_calls(self) -> int:
        return sum(1 for call in self.tool_calls if call.get("is_error") is True)

    def to_dict(self, *, include_timeline: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "status": self.status,
            "phase": self.phase,
            "event_count": self.event_count,
            "tool_calls": self.tool_calls,
            "failed_tool_calls": self.failed_tool_calls,
            "permission_requests": self.permission_requests,
            "context_builds": self.context_builds,
            "loop_guards": self.loop_guards,
            "errors": self.errors,
            "usage": self.usage,
            "final": self.final,
            "checkpoint": self.checkpoint,
            "long_run": self.long_run,
        }
        if include_timeline:
            out["timeline"] = self.timeline
        return out

    def to_markdown(self) -> str:
        lines = [
            f"# Linch Run Report: {self.run_id or '<unknown>'}",
            "",
            f"- status: {self.status}",
            f"- phase: {self.phase or '<none>'}",
            f"- events: {self.event_count}",
            f"- tool calls: {len(self.tool_calls)} ({self.failed_tool_calls} failed)",
            f"- permission requests: {len(self.permission_requests)}",
            f"- context builds: {len(self.context_builds)}",
            f"- loop guard events: {len(self.loop_guards)}",
            f"- errors: {len(self.errors)}",
        ]
        if self.long_run:
            context = self.long_run.get("context", {})
            memory = self.long_run.get("memory", {})
            quality = self.long_run.get("quality", {})
            lines.extend(
                [
                    "",
                    "## Long-Run Signals",
                    f"- context trimmed builds: {context.get('trimmed_builds', 0)}",
                    f"- max context tokens: {context.get('max_used_tokens')}",
                    f"- memory searches: {memory.get('searches', 0)}",
                    f"- memory result ids: {', '.join(memory.get('result_ids', []))}",
                    f"- recovery hints: {quality.get('recovery_hints', 0)}",
                ]
            )
        if self.final is not None:
            lines.extend(
                [
                    "",
                    "## Final",
                    f"- subtype: {self.final.get('subtype')}",
                    f"- stop_reason: {self.final.get('stop_reason')}",
                    f"- duration_ms: {self.final.get('duration_ms')}",
                    f"- total_cost_usd: {self.final.get('total_cost_usd')}",
                ]
            )
            text = self.final.get("final_text")
            if text:
                lines.extend(["", str(text)])
        if self.tool_calls:
            lines.extend(
                [
                    "",
                    "## Tool Calls",
                    "",
                    "| Tool | Summary | Error | Duration ms |",
                    "|---|---|---:|---:|",
                ]
            )
            for call in self.tool_calls:
                lines.append(
                    "| {tool} | {summary} | {error} | {duration} |".format(
                        tool=call.get("tool_name", ""),
                        summary=str(call.get("summary", "")).replace("|", "\\|"),
                        error=call.get("is_error", False),
                        duration=call.get("duration_ms", 0),
                    )
                )
        return "\n".join(lines)


def build_run_report(
    events: Sequence[Event | StoredRunEvent],
    *,
    run: RunRecord | None = None,
) -> RunReport:
    run_id = run.id if run is not None else ""
    session_id = run.session_id if run is not None else ""
    status = run.status if run is not None else "unknown"
    status_from_run = run is not None
    phase = run.checkpoint.phase if run is not None and run.checkpoint is not None else None
    checkpoint = (
        checkpoint_to_dict(run.checkpoint)
        if run is not None and run.checkpoint is not None
        else None
    )
    tool_starts: dict[str, dict[str, Any]] = {}
    tool_calls: list[dict[str, Any]] = []
    permission_requests: list[dict[str, Any]] = []
    context_builds: list[dict[str, Any]] = []
    loop_guards: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    usage: dict[str, Any] | None = None
    final: dict[str, Any] | None = None
    timeline: list[dict[str, Any]] = []

    for index, item in enumerate(events, start=1):
        seq, appended_at, event = _event_parts(item, index)
        timeline.append(
            {
                "seq": seq,
                "appended_at": appended_at,
                "type": event.type,
                "event": event_to_dict(event),
            }
        )
        if isinstance(event, SystemEvent):
            run_id = run_id or event.run_id
            session_id = session_id or event.session_id
            if status == "unknown":
                status = "running"
        elif isinstance(event, ToolCallStartEvent):
            tool_starts[event.tool_use_id] = {
                "tool_use_id": event.tool_use_id,
                "tool_name": event.tool_name,
                "input": event.input,
                "summary": event.summary,
            }
        elif isinstance(event, ToolCallEndEvent):
            call = dict(tool_starts.get(event.tool_use_id, {}))
            call.update(
                {
                    "tool_use_id": event.tool_use_id,
                    "tool_name": event.tool_name,
                    "result": event.result,
                    "is_error": event.is_error,
                    "duration_ms": event.duration_ms,
                }
            )
            if event.tool_result is not None:
                call["tool_result"] = tool_result_to_dict(event.tool_result)
            tool_calls.append(call)
        elif isinstance(event, PermissionRequestEvent):
            for req in event.requests:
                permission_requests.append(
                    {
                        "tool_use_id": req.tool_use_id,
                        "tool_name": req.tool_name,
                        "input": req.input,
                        "summary": req.summary,
                    }
                )
        elif isinstance(event, ContextBuildEvent):
            context_builds.append(
                {
                    "system_blocks": event.system_blocks,
                    "messages": event.messages,
                    "selected_tools": event.selected_tools,
                    "budget": dict(event.budget),
                    "metadata": dict(event.metadata),
                }
            )
        elif isinstance(event, LoopGuardEvent):
            loop_guards.append(
                {"reason": event.reason, "detail": event.detail, "action": event.action}
            )
        elif isinstance(event, ErrorEvent):
            errors.append(
                dict(event.error) if isinstance(event.error, dict) else {"error": str(event.error)}
            )
        elif isinstance(event, UsageEvent):
            usage = {
                "usage": usage_to_dict(event.usage),
                "cumulative": usage_to_dict(event.cumulative),
                "cost_usd": event.cost_usd,
                "cumulative_cost_usd": event.cumulative_cost_usd,
            }
        elif isinstance(event, ResultEvent):
            final = {
                "subtype": event.subtype,
                "stop_reason": event.stop_reason,
                "duration_ms": event.duration_ms,
                "final_text": event.final_text,
                "structured_output": event.structured_output,
                "structured_error": event.structured_error,
                "total_usage": usage_to_dict(event.total_usage),
                "total_cost_usd": event.total_cost_usd,
            }
            if not status_from_run and status in {"unknown", "running"}:
                status = "completed" if event.subtype == "success" else event.subtype

    return RunReport(
        run_id=run_id,
        session_id=session_id,
        status=status,
        phase=phase,
        event_count=len(events),
        tool_calls=tool_calls,
        permission_requests=permission_requests,
        context_builds=context_builds,
        loop_guards=loop_guards,
        errors=errors,
        usage=usage,
        final=final,
        checkpoint=checkpoint,
        timeline=timeline,
        long_run=_long_run_summary(
            context_builds=context_builds,
            tool_calls=tool_calls,
            checkpoint=checkpoint,
            final=final,
        ),
    )


async def load_run_report(run_store: RunStore, run_id: str) -> RunReport:
    run = await run_store.load_run(run_id)
    events = await run_store.load_events(run_id)
    return build_run_report(events, run=run)


def _event_parts(item: Event | StoredRunEvent, fallback_seq: int) -> tuple[int, str | None, Event]:
    if isinstance(item, StoredRunEvent):
        return item.seq, item.appended_at, item.event
    return fallback_seq, None, item


def _long_run_summary(
    *,
    context_builds: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
    checkpoint: dict[str, Any] | None,
    final: dict[str, Any] | None,
) -> dict[str, Any]:
    selected_tool_counts: dict[str, int] = {}
    metadata_keys: set[str] = set()
    trimmed_builds = 0
    max_used_tokens: int | None = None

    for build in context_builds:
        selected_tools = build.get("selected_tools")
        if isinstance(selected_tools, list):
            for tool in selected_tools:
                if isinstance(tool, str):
                    selected_tool_counts[tool] = selected_tool_counts.get(tool, 0) + 1
        metadata = build.get("metadata")
        if isinstance(metadata, dict):
            metadata_keys.update(str(key) for key in metadata)
        budget = build.get("budget")
        if isinstance(budget, dict):
            if budget.get("trimmed") is True:
                trimmed_builds += 1
            used = budget.get("used_tokens")
            if isinstance(used, int):
                max_used_tokens = used if max_used_tokens is None else max(max_used_tokens, used)

    memory_result_ids: set[str] = set()
    memory_namespaces: set[str] = set()
    memory_tier_counts: dict[str, int] = {}
    memory_searches = 0
    memory_upserts = 0
    recovery_hints = 0

    for call in tool_calls:
        tool_name = str(call.get("tool_name", ""))
        tool_result = call.get("tool_result")
        if not isinstance(tool_result, dict):
            tool_result = {}
        metadata = tool_result.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}

        if tool_result.get("recovery_hint"):
            recovery_hints += 1

        if tool_name == "SearchMemory":
            memory_searches += 1
            _add_strings(memory_result_ids, metadata.get("result_ids"))
            namespace = metadata.get("namespace")
            if isinstance(namespace, str):
                memory_namespaces.add(namespace)
            has_tool_tier_counts = isinstance(metadata.get("tier_counts"), dict)
            _merge_counts(memory_tier_counts, metadata.get("tier_counts"))
            citations = tool_result.get("citations")
            if isinstance(citations, list):
                for citation in citations:
                    if not isinstance(citation, dict):
                        continue
                    citation_id = citation.get("id")
                    if isinstance(citation_id, str):
                        memory_result_ids.add(citation_id)
                    citation_metadata = citation.get("metadata")
                    if isinstance(citation_metadata, dict) and not has_tool_tier_counts:
                        tier = citation_metadata.get("tier")
                        if isinstance(tier, str):
                            memory_tier_counts[tier] = memory_tier_counts.get(tier, 0) + 1
        elif tool_name == "UpsertMemory":
            memory_upserts += 1
            item_id = metadata.get("id")
            if isinstance(item_id, str):
                memory_result_ids.add(item_id)
            namespace = metadata.get("namespace")
            if isinstance(namespace, str):
                memory_namespaces.add(namespace)

    return {
        "context": {
            "builds": len(context_builds),
            "trimmed_builds": trimmed_builds,
            "max_used_tokens": max_used_tokens,
            "selected_tool_counts": dict(sorted(selected_tool_counts.items())),
            "metadata_keys": sorted(metadata_keys),
        },
        "memory": {
            "searches": memory_searches,
            "upserts": memory_upserts,
            "result_ids": sorted(memory_result_ids),
            "namespaces": sorted(memory_namespaces),
            "tier_counts": dict(sorted(memory_tier_counts.items())),
        },
        "quality": {
            "failed_tool_calls": sum(1 for call in tool_calls if call.get("is_error") is True),
            "recovery_hints": recovery_hints,
            "completed": final is not None and final.get("subtype") == "success",
            "total_cost_usd": final.get("total_cost_usd") if final is not None else None,
        },
        "resume": {
            "has_checkpoint": checkpoint is not None,
            "phase": checkpoint.get("phase") if checkpoint is not None else None,
            "turn_index": checkpoint.get("turn_index") if checkpoint is not None else None,
        },
    }


def _add_strings(target: set[str], value: Any) -> None:
    if isinstance(value, str):
        target.add(value)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                target.add(item)


def _merge_counts(target: dict[str, int], value: Any) -> None:
    if not isinstance(value, dict):
        return
    for key, count in value.items():
        if isinstance(key, str) and isinstance(count, int):
            target[key] = target.get(key, 0) + count
