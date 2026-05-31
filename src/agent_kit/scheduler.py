from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from .abort import AbortContext, throw_if_aborted
from .errors import AbortError
from .events import (
    Event,
    PermissionRequestEvent,
    PermissionRequestItem,
    SkillCompletedEvent,
    SkillInvokedEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from .permissions import PendingToolCall, PermissionDecision
from .tools import ToolContext
from .types import ToolResultBlock, ToolUseBlock


@dataclass(slots=True)
class ResolvedCall:
    id: str
    block: ToolUseBlock
    tool: Any | None
    input: dict[str, Any]
    summary: str
    is_immediate_error: bool
    immediate_error_reason: str | None = None


def _resolve_call(block: ToolUseBlock, tools: Any, cwd: str) -> ResolvedCall:
    if block.input.get("__invalid_json"):
        raw = str(block.input.get("raw", ""))
        return ResolvedCall(
            id=block.id,
            block=block,
            tool=None,
            input=block.input,
            summary=f"{block.name}(invalid JSON)",
            is_immediate_error=True,
            immediate_error_reason=f"Tool input was not valid JSON: {raw}",
        )

    tool = tools.get(block.name) if tools else None
    if tool is None:
        return ResolvedCall(
            id=block.id,
            block=block,
            tool=None,
            input=block.input,
            summary=f"{block.name}(unknown)",
            is_immediate_error=True,
            immediate_error_reason=f"Tool '{block.name}' is not registered",
        )

    try:
        validated = tool.validate(block.input)
    except Exception as exc:
        return ResolvedCall(
            id=block.id,
            block=block,
            tool=tool,
            input=block.input,
            summary=f"{block.name}(invalid input)",
            is_immediate_error=True,
            immediate_error_reason=str(exc),
        )

    try:
        summary = tool.summarize(validated)
    except Exception:
        summary = f"{block.name}(...)"

    return ResolvedCall(
        id=block.id,
        block=block,
        tool=tool,
        input=validated,
        summary=summary,
        is_immediate_error=False,
    )


def _tool_name(call: ResolvedCall) -> str:
    return call.tool.name if call.tool else call.block.name


def _effective_input(call: ResolvedCall, decision: PermissionDecision) -> dict[str, Any]:
    if decision.decision == "allow" and decision.updated_input is not None:
        return decision.updated_input
    return call.input


def _skill_name_from_call(call: ResolvedCall) -> str | None:
    if call.is_immediate_error:
        return None
    if call.tool is None or call.tool.name != "Skill":
        return None
    raw = call.input.get("skill")
    if not isinstance(raw, str) or raw.strip() == "":
        return None
    return raw[1:] if raw.startswith("/") else raw


async def _execute_one(
    call: ResolvedCall,
    decision: PermissionDecision,
    agent: Any,
    session: Any,
    signal: AbortContext,
) -> tuple[ToolResultBlock, int]:
    throw_if_aborted(signal)

    if call.is_immediate_error:
        return (
            ToolResultBlock(
                tool_use_id=call.id,
                content=call.immediate_error_reason or "unknown error",
                is_error=True,
            ),
            0,
        )

    if decision.decision == "deny":
        return (
            ToolResultBlock(
                tool_use_id=call.id,
                content=f"Tool call denied: {decision.reason or 'permission denied'}",
                is_error=True,
            ),
            0,
        )

    tool = call.tool
    started = time.perf_counter()
    try:
        ctx = ToolContext(
            cwd=agent.cwd,
            session_id=session.id,
            run_id=session.active_run_id or "unknown",
            session_store=session.store,
            signal=signal,
            file_read_tracker=getattr(session, "file_read_tracker", None),
            deps=getattr(session, "run_deps", None),
        )
        result = await tool.execute(_effective_input(call, decision), ctx)
        elapsed = int((time.perf_counter() - started) * 1000)
        if result.duration_ms <= 0:
            result.duration_ms = elapsed
        return (
            ToolResultBlock(
                tool_use_id=call.id,
                content=result.content,
                is_error=result.is_error,
            ),
            result.duration_ms,
        )
    except AbortError:
        raise
    except Exception as exc:
        return (
            ToolResultBlock(
                tool_use_id=call.id,
                content=f"Tool failed: {exc}",
                is_error=True,
            ),
            int((time.perf_counter() - started) * 1000),
        )


def _partition_batches(
    resolved: list[ResolvedCall],
    decisions: list[PermissionDecision],
) -> list[dict[str, Any]]:
    batches: list[dict[str, Any]] = []
    for i, call in enumerate(resolved):
        parallel = not call.is_immediate_error and call.tool is not None and call.tool.parallel_safe
        if batches and batches[-1]["parallel"] and parallel:
            batches[-1]["calls"].append((call, i, decisions[i]))
        else:
            batches.append(
                {
                    "parallel": parallel,
                    "calls": [(call, i, decisions[i])],
                }
            )
    return batches


async def execute_tool_calls(
    blocks: list[ToolUseBlock],
    agent: Any,
    session: Any,
    signal: AbortContext,
) -> AsyncIterator[Event]:
    if not blocks:
        return

    effective_tools = getattr(session, "tools_override", None) or agent.tools
    resolved = [_resolve_call(b, effective_tools, agent.cwd) for b in blocks]

    # First pass: synchronous evaluate()
    decisions: list[PermissionDecision] = []
    ask_indices: list[int] = []

    for i, call in enumerate(resolved):
        if call.is_immediate_error:
            decisions.append(
                PermissionDecision(decision="deny", reason=call.immediate_error_reason)
            )
            continue

        tool_obj = call.tool
        initial = agent.permission_engine.evaluate(
            PendingToolCall(
                tool_use_id=call.id,
                tool=tool_obj,
                input=call.input,
                cwd=agent.cwd,
            )
        )

        if initial.decision == "ask":
            allowed_tools = getattr(session, "current_turn_allowed_tools", None)
            if allowed_tools and tool_obj.name in allowed_tools:
                decisions.append(PermissionDecision(decision="allow"))
            else:
                ask_indices.append(i)
                decisions.append(initial)
        else:
            decisions.append(initial)

    # Emit single aggregated PermissionRequestEvent before resolve
    if ask_indices:
        items: list[PermissionRequestItem] = []
        for idx in ask_indices:
            call = resolved[idx]
            items.append(
                PermissionRequestItem(
                    tool_use_id=call.id,
                    tool_name=_tool_name(call),
                    input=call.input,
                    summary=call.summary,
                )
            )
        yield PermissionRequestEvent(requests=items)

    # Second pass: async resolve()
    for idx in ask_indices:
        call = resolved[idx]
        try:
            decisions[idx] = await agent.permission_engine.resolve(
                PendingToolCall(
                    tool_use_id=call.id,
                    tool=call.tool,
                    input=call.input,
                    cwd=agent.cwd,
                ),
                signal,
            )
        except Exception:
            decisions[idx] = PermissionDecision(
                decision="deny",
                reason=f"Permission resolution failed for {_tool_name(call)}",
            )

    # Partition into batches by parallelSafe
    batches = _partition_batches(resolved, decisions)

    # Execute batches in order
    result_pairs: list[tuple[int, ToolResultBlock]] = []
    for batch in batches:
        throw_if_aborted(signal)

        if not batch["parallel"]:
            # Serial lane — one call at a time
            for call, idx, decision in batch["calls"]:
                skill_name = _skill_name_from_call(call)
                if skill_name is not None:
                    args = call.input.get("args")
                    yield SkillInvokedEvent(
                        name=skill_name,
                        args=args if isinstance(args, str) else None,
                    )
                yield ToolCallStartEvent(
                    tool_use_id=call.id,
                    tool_name=_tool_name(call),
                    input=_effective_input(call, decision),
                    summary=call.summary,
                )
                try:
                    result, duration_ms = await _execute_one(call, decision, agent, session, signal)
                except AbortError:
                    yield ToolCallEndEvent(
                        tool_use_id=call.id,
                        tool_name=_tool_name(call),
                        result="aborted",
                        is_error=True,
                        duration_ms=0,
                    )
                    raise
                yield ToolCallEndEvent(
                    tool_use_id=call.id,
                    tool_name=_tool_name(call),
                    result=str(result.content),
                    is_error=result.is_error,
                    duration_ms=duration_ms,
                )
                if skill_name is not None:
                    yield SkillCompletedEvent(name=skill_name, is_error=result.is_error)
                result_pairs.append((idx, result))
        else:
            # Parallel lane — all starts first, then run concurrently, then all ends
            skill_names: dict[str, str | None] = {}
            for call, _idx, decision in batch["calls"]:
                skill_name = _skill_name_from_call(call)
                skill_names[call.id] = skill_name
                if skill_name is not None:
                    args = call.input.get("args")
                    yield SkillInvokedEvent(
                        name=skill_name,
                        args=args if isinstance(args, str) else None,
                    )
                yield ToolCallStartEvent(
                    tool_use_id=call.id,
                    tool_name=_tool_name(call),
                    input=_effective_input(call, decision),
                    summary=call.summary,
                )

            tasks = [
                asyncio.ensure_future(_execute_one(call, decision, agent, session, signal))
                for call, _idx, decision in batch["calls"]
            ]

            started_ids: set[str] = set()
            id_to_call: dict[str, ResolvedCall] = {}
            for call, *_ in batch["calls"]:
                started_ids.add(call.id)
                id_to_call[call.id] = call

            try:
                results = await asyncio.gather(*tasks)
            except AbortError:
                for t in tasks:
                    t.cancel()
                # Orphan-bracket synthesis
                finished_ids: set[str] = set()
                for t in tasks:
                    if t.done() and not t.cancelled():
                        try:
                            r, _ = t.result()
                            finished_ids.add(r.tool_use_id)
                        except Exception:
                            pass
                for tid in started_ids:
                    if tid not in finished_ids:
                        orphan_call = id_to_call.get(tid)
                        yield ToolCallEndEvent(
                            tool_use_id=tid,
                            tool_name=(_tool_name(orphan_call) if orphan_call else "unknown"),
                            result="aborted",
                            is_error=True,
                            duration_ms=0,
                        )
                        sn = skill_names.get(tid)
                        if sn is not None:
                            yield SkillCompletedEvent(name=sn, is_error=True)
                raise

            for (call, idx, _), packed in zip(batch["calls"], results, strict=True):
                result, duration_ms = packed
                yield ToolCallEndEvent(
                    tool_use_id=call.id,
                    tool_name=_tool_name(call),
                    result=str(result.content),
                    is_error=result.is_error,
                    duration_ms=duration_ms,
                )
                sn = skill_names.get(call.id)
                if sn is not None:
                    yield SkillCompletedEvent(name=sn, is_error=result.is_error)
                result_pairs.append((idx, result))
