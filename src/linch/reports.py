from __future__ import annotations

import math
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
    summary: dict[str, Any] = field(default_factory=dict)
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
            "summary": self.summary,
            "long_run": self.long_run,
        }
        if include_timeline:
            out["timeline"] = self.timeline
        return out

    def to_markdown(self) -> str:
        usage = self.summary.get("usage", {})
        tools = self.summary.get("tools", {})
        context = self.summary.get("context", {})
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
            f"- duration_ms: {self.summary.get('duration_ms')}",
            f"- total_tokens: {usage.get('total_tokens')}",
            f"- total_cost_usd: {usage.get('total_cost_usd')}",
        ]
        if self.summary:
            slowest = tools.get("slowest_tool")
            lines.extend(
                [
                    "",
                    "## Summary",
                    f"- tool duration ms: total={tools.get('total_duration_ms', 0)} "
                    f"avg={tools.get('average_duration_ms', 0)} "
                    f"max={tools.get('max_duration_ms', 0)}",
                    f"- tool error rate: {tools.get('error_rate', 0)}",
                    f"- cache read ratio: {usage.get('cache_read_ratio', 0)}",
                    f"- max context utilization: {context.get('max_utilization')}",
                    f"- context pressure: {context.get('pressure', 'none')}",
                ]
            )
            if isinstance(slowest, dict):
                lines.append(
                    "- slowest tool: {tool} ({duration}ms)".format(
                        tool=slowest.get("tool_name", ""),
                        duration=slowest.get("duration_ms", 0),
                    )
                )
            top_slowest = tools.get("top_slowest")
            if isinstance(top_slowest, list) and top_slowest:
                lines.extend(
                    [
                        "",
                        "## Top Slow Tools",
                        "",
                        "| Tool | Summary | Error | Duration ms |",
                        "|---|---|---:|---:|",
                    ]
                )
                for call in top_slowest:
                    if not isinstance(call, dict):
                        continue
                    lines.append(
                        "| {tool} | {summary} | {error} | {duration} |".format(
                            tool=_markdown_cell(call.get("tool_name", "")),
                            summary=_markdown_cell(call.get("summary", "")),
                            error=call.get("is_error", False),
                            duration=call.get("duration_ms", 0),
                        )
                    )
            top_failures = tools.get("top_failures")
            if isinstance(top_failures, list) and top_failures:
                lines.extend(
                    [
                        "",
                        "## Failing Tools",
                        "",
                        "| Tool | Summary | Duration ms | Detail |",
                        "|---|---|---:|---|",
                    ]
                )
                for call in top_failures:
                    if not isinstance(call, dict):
                        continue
                    detail = call.get("error") or call.get("result") or ""
                    lines.append(
                        "| {tool} | {summary} | {duration} | {detail} |".format(
                            tool=_markdown_cell(call.get("tool_name", "")),
                            summary=_markdown_cell(call.get("summary", "")),
                            duration=call.get("duration_ms"),
                            detail=_markdown_cell(detail),
                        )
                    )
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

    long_run = _long_run_summary(
        context_builds=context_builds,
        tool_calls=tool_calls,
        checkpoint=checkpoint,
        final=final,
    )
    summary = _report_summary(
        timeline=timeline,
        tool_calls=tool_calls,
        permission_requests=permission_requests,
        context_builds=context_builds,
        loop_guards=loop_guards,
        errors=errors,
        usage=usage,
        final=final,
        long_run=long_run,
    )

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
        summary=summary,
        timeline=timeline,
        long_run=long_run,
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


def _report_summary(
    *,
    timeline: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
    permission_requests: list[dict[str, Any]],
    context_builds: list[dict[str, Any]],
    loop_guards: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    usage: dict[str, Any] | None,
    final: dict[str, Any] | None,
    long_run: dict[str, Any],
) -> dict[str, Any]:
    event_counts: dict[str, int] = {}
    for item in timeline:
        event_type = item.get("type")
        if isinstance(event_type, str):
            event_counts[event_type] = event_counts.get(event_type, 0) + 1

    tool_durations = [
        duration
        for duration in (_maybe_int(call.get("duration_ms")) for call in tool_calls)
        if duration is not None
    ]
    failed_tools = sum(1 for call in tool_calls if call.get("is_error") is True)
    slowest_tool = _slowest_tool(tool_calls)
    top_slowest = _top_slowest_tools(tool_calls)
    top_failures = _top_failed_tools(tool_calls)
    usage_source = _usage_source(usage=usage, final=final)
    context_summary = _context_summary(context_builds, long_run)
    recovery_summary = _recovery_summary(timeline, tool_calls)

    return {
        "duration_ms": final.get("duration_ms") if final is not None else None,
        "event_counts": dict(sorted(event_counts.items())),
        "first_event_at": timeline[0].get("appended_at") if timeline else None,
        "last_event_at": timeline[-1].get("appended_at") if timeline else None,
        "usage": {
            "input_tokens": usage_source.get("input_tokens", 0),
            "output_tokens": usage_source.get("output_tokens", 0),
            "cache_read_tokens": usage_source.get("cache_read_tokens", 0),
            "cache_creation_tokens": usage_source.get("cache_creation_tokens", 0),
            "total_tokens": _total_tokens(usage_source),
            "cache_read_ratio": _cache_read_ratio(usage_source),
            "total_cost_usd": final.get("total_cost_usd") if final is not None else None,
        },
        "tools": {
            "total": len(tool_calls),
            "failed": failed_tools,
            "error_rate": round(failed_tools / len(tool_calls), 4) if tool_calls else 0.0,
            "total_duration_ms": sum(tool_durations),
            "average_duration_ms": round(sum(tool_durations) / len(tool_durations), 2)
            if tool_durations
            else 0,
            "max_duration_ms": max(tool_durations) if tool_durations else 0,
            "slowest_tool": slowest_tool,
            "top_slowest": top_slowest,
            "top_failures": top_failures,
            "by_name": _tool_counts(tool_calls),
            "by_name_errors": _tool_error_counts(tool_calls),
        },
        "context": context_summary,
        "recovery": recovery_summary,
        "risk": {
            "permission_requests": len(permission_requests),
            "loop_guards": len(loop_guards),
            "errors": len(errors),
            "recovery_hints": long_run.get("quality", {}).get("recovery_hints", 0),
            "model_fallbacks": recovery_summary["model_fallbacks"],
            "verification_retries": recovery_summary["verification_retries"],
            "hook_retries": recovery_summary["hook_retries"],
        },
    }


def _usage_source(*, usage: dict[str, Any] | None, final: dict[str, Any] | None) -> dict[str, Any]:
    if final is not None and isinstance(final.get("total_usage"), dict):
        return final["total_usage"]
    if usage is not None and isinstance(usage.get("cumulative"), dict):
        return usage["cumulative"]
    return {}


def _total_tokens(usage: dict[str, Any]) -> int:
    return sum(
        _maybe_int(usage.get(key)) or 0
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
        )
    )


def _cache_read_ratio(usage: dict[str, Any]) -> float:
    cache_read = _maybe_int(usage.get("cache_read_tokens")) or 0
    prompt_tokens = sum(
        _maybe_int(usage.get(key)) or 0
        for key in ("input_tokens", "cache_read_tokens", "cache_creation_tokens")
    )
    return round(cache_read / prompt_tokens, 4) if prompt_tokens else 0.0


def _recovery_summary(
    timeline: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
) -> dict[str, Any]:
    compactions = 0
    compaction_tokens_saved = 0
    model_fallbacks = 0
    fallback_paths: list[dict[str, str]] = []
    verification_retries = 0
    hook_retries = 0

    for item in timeline:
        event_type = item.get("type")
        event = item.get("event")
        if not isinstance(event, dict):
            event = {}
        if event_type == "compaction":
            compactions += 1
            before = _maybe_int(event.get("tokens_before")) or 0
            after = _maybe_int(event.get("tokens_after")) or 0
            compaction_tokens_saved += max(before - after, 0)
        elif event_type == "model_fallback":
            model_fallbacks += 1
            fallback_paths.append(
                {
                    "from_model": str(event.get("from_model", "")),
                    "to_model": str(event.get("to_model", "")),
                    "reason": str(event.get("reason", "")),
                }
            )
        elif event_type == "verification" and event.get("action") == "retry":
            verification_retries += 1
        elif event_type == "hook" and event.get("action") == "retry":
            hook_retries += 1

    result_offloads = _result_offload_count(tool_calls)
    return {
        "compactions": compactions,
        "compaction_tokens_saved": compaction_tokens_saved,
        "model_fallbacks": model_fallbacks,
        "fallback_paths": fallback_paths,
        "verification_retries": verification_retries,
        "hook_retries": hook_retries,
        "result_offloads": result_offloads,
        "offload_hit_rate": round(result_offloads / len(tool_calls), 4) if tool_calls else 0.0,
    }


def _result_offload_count(tool_calls: list[dict[str, Any]]) -> int:
    total = 0
    for call in tool_calls:
        tool_result = call.get("tool_result")
        if not isinstance(tool_result, dict):
            continue
        metadata = tool_result.get("metadata")
        if tool_result.get("truncated") is True and isinstance(metadata, dict):
            total += 1 if isinstance(metadata.get("offloaded_to"), str) else 0
    return total


def _slowest_tool(tool_calls: list[dict[str, Any]]) -> dict[str, Any] | None:
    slowest: dict[str, Any] | None = None
    slowest_duration = -1
    for call in tool_calls:
        duration = _maybe_int(call.get("duration_ms"))
        if duration is None or duration < slowest_duration:
            continue
        slowest_duration = duration
        slowest = {
            "tool_use_id": call.get("tool_use_id", ""),
            "tool_name": call.get("tool_name", ""),
            "summary": call.get("summary", ""),
            "duration_ms": duration,
            "is_error": call.get("is_error", False),
        }
    return slowest


def _top_slowest_tools(tool_calls: list[dict[str, Any]], *, limit: int = 5) -> list[dict[str, Any]]:
    ranked: list[tuple[int, int, dict[str, Any]]] = []
    for index, call in enumerate(tool_calls):
        duration = _maybe_int(call.get("duration_ms"))
        if duration is None:
            continue
        ranked.append(
            (
                -duration,
                index,
                {
                    "tool_use_id": call.get("tool_use_id", ""),
                    "tool_name": call.get("tool_name", ""),
                    "summary": call.get("summary", ""),
                    "duration_ms": duration,
                    "is_error": call.get("is_error", False),
                },
            )
        )
    return [item for _, _, item in sorted(ranked)[:limit]]


def _top_failed_tools(tool_calls: list[dict[str, Any]], *, limit: int = 5) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for call in tool_calls:
        if call.get("is_error") is not True:
            continue
        result = _compact_tool_text(call)
        failure = {
            "tool_use_id": call.get("tool_use_id", ""),
            "tool_name": call.get("tool_name", ""),
            "summary": call.get("summary", ""),
            "duration_ms": _maybe_int(call.get("duration_ms")),
        }
        if result:
            failure["result"] = result
            failure["error"] = result
        failures.append(failure)
        if len(failures) >= limit:
            break
    return failures


def _tool_counts(tool_calls: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for call in tool_calls:
        tool_name = call.get("tool_name")
        if isinstance(tool_name, str):
            counts[tool_name] = counts.get(tool_name, 0) + 1
    return dict(sorted(counts.items()))


def _tool_error_counts(tool_calls: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for call in tool_calls:
        if call.get("is_error") is not True:
            continue
        tool_name = call.get("tool_name")
        if isinstance(tool_name, str):
            counts[tool_name] = counts.get(tool_name, 0) + 1
    return dict(sorted(counts.items()))


def _context_summary(
    context_builds: list[dict[str, Any]],
    long_run: dict[str, Any],
) -> dict[str, Any]:
    max_utilization: float | None = None
    max_used_tokens: int | None = None
    max_tokens_seen: int | None = None

    for build in context_builds:
        budget = build.get("budget")
        if not isinstance(budget, dict):
            continue
        used = _maybe_int(budget.get("used_tokens"))
        maximum = _maybe_int(budget.get("max_tokens"))
        if used is not None:
            max_used_tokens = used if max_used_tokens is None else max(max_used_tokens, used)
        if maximum is not None:
            max_tokens_seen = maximum if max_tokens_seen is None else max(max_tokens_seen, maximum)
        if used is not None and maximum and maximum > 0:
            ratio = round(used / maximum, 4)
            max_utilization = ratio if max_utilization is None else max(max_utilization, ratio)

    context = long_run.get("context", {})
    return {
        "builds": len(context_builds),
        "trimmed_builds": context.get("trimmed_builds", 0),
        "max_used_tokens": max_used_tokens,
        "max_tokens_seen": max_tokens_seen,
        "max_utilization": max_utilization,
        "pressure": _context_pressure(max_utilization),
    }


def _context_pressure(max_utilization: float | None) -> str:
    if max_utilization is None:
        return "none"
    if max_utilization > 1.0:
        return "over"
    if max_utilization >= 0.9:
        return "high"
    if max_utilization >= 0.75:
        return "moderate"
    return "none"


def _compact_tool_text(call: dict[str, Any], *, limit: int = 160) -> str:
    result = call.get("result")
    if isinstance(result, str) and result:
        return _compact_text(result, limit=limit)
    tool_result = call.get("tool_result")
    if not isinstance(tool_result, dict):
        return ""
    for key in ("summary", "recovery_hint", "content"):
        value = tool_result.get(key)
        if isinstance(value, str) and value:
            return _compact_text(value, limit=limit)
    return ""


def _compact_text(value: str, *, limit: int) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return f"{text[: limit - 3]}..."


def _markdown_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _maybe_int(value: Any) -> int | None:
    # Coerce finite floats too: a provider/tool reporting a float duration_ms or
    # token count would otherwise be silently dropped from the report aggregates.
    # NaN/Infinity (which json.loads accepts, so they survive a persisted-event
    # round-trip) are dropped rather than coerced — int(nan)/int(inf) would raise
    # and crash report building, which must stay a non-throwing read model.
    if isinstance(value, float):
        return int(value) if math.isfinite(value) else None
    return value if isinstance(value, int) else None


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
