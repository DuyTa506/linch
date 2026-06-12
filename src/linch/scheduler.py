from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from typing import Any, cast

from .abort import AbortContext, throw_if_aborted
from .errors import AbortError, ToolTimeoutError
from .events import (
    Event,
    PermissionRequestEvent,
    PermissionRequestItem,
    SkillCompletedEvent,
    SkillInvokedEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from .hooks import (
    HookDispatcher,
    HookEvent,
    PostToolUseContext,
    PreToolUseContext,
)
from .permissions import PendingToolCall, PermissionDecision
from .permissions.keys import permission_decision_key as _permission_key
from .providers.retry import RetryOptions, _delay_for_error
from .tools import ResourceAccess, ToolContext, ToolResult
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


@dataclass(slots=True)
class ToolExecutionOutcome:
    block: ToolResultBlock
    tool_result: ToolResult
    duration_ms: int


def _tool_result_error(content: str, duration_ms: int = 0) -> ToolResult:
    return ToolResult(content=content, is_error=True, duration_ms=duration_ms)


async def _maybe_offload_block(
    result: ToolResult,
    *,
    call: ResolvedCall,
    agent: Any,
    session: Any,
) -> ToolResult:
    """Return the provider-facing block for *result*, offloading oversized
    payloads when a filesystem backend + offload config are configured.

    Returns *result* unchanged when offload is disabled or not applicable
    (``maybe_offload`` is a no-op on error / filesystem-tool results)."""
    _fs = getattr(session, "filesystem", None)
    _offload_cfg = getattr(agent, "result_offload", None)
    if _fs is None or _offload_cfg is None:
        return result
    from .filesystem.offload import maybe_offload

    offload_result = replace(
        result,
        metadata=dict(result.metadata),
        citations=list(result.citations),
        attachments=list(result.attachments),
    )
    return await maybe_offload(
        offload_result,
        tool_name=_tool_name(call),
        call_id=call.id,
        backend=_fs,
        config=_offload_cfg,
        token_estimator=getattr(agent, "token_estimator", None),
        model=agent.model,
    )


def _execution_outcome(
    call_id: str,
    tool_result: ToolResult,
    block_result: ToolResult | None = None,
) -> ToolExecutionOutcome:
    provider_result = block_result or tool_result
    return ToolExecutionOutcome(
        block=ToolResultBlock(
            tool_use_id=call_id,
            content=provider_result.content,
            is_error=provider_result.is_error,
        ),
        tool_result=tool_result,
        duration_ms=tool_result.duration_ms,
    )


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
    return _skill_name_from_input(call, call.input)


def _skill_name_from_input(call: ResolvedCall, input: dict[str, Any]) -> str | None:
    if call.is_immediate_error:
        return None
    if call.tool is None or call.tool.name != "Skill":
        return None
    raw = input.get("skill")
    if not isinstance(raw, str) or raw.strip() == "":
        return None
    return raw[1:] if raw.startswith("/") else raw


async def _execute_one(
    call: ResolvedCall,
    decision: PermissionDecision,
    agent: Any,
    session: Any,
    signal: AbortContext,
    *,
    turn_index: int | None = None,
    middleware_error: str | None = None,
) -> ToolExecutionOutcome:
    throw_if_aborted(signal)

    if call.is_immediate_error:
        return _execution_outcome(
            call.id,
            _tool_result_error(call.immediate_error_reason or "unknown error"),
        )

    if decision.decision == "deny":
        return _execution_outcome(
            call.id,
            _tool_result_error(f"Tool call denied: {decision.reason or 'permission denied'}"),
        )

    if middleware_error is not None:
        return _execution_outcome(call.id, _tool_result_error(middleware_error))

    tool = call.tool
    result_timeout_ms = _tool_timeout_ms(agent, tool)
    opts = _retry_options(agent)
    max_attempts = opts.max_attempts if opts is not None else 1
    started = time.perf_counter()
    last_exc: Exception | None = None

    ctx = ToolContext(
        cwd=agent.cwd,
        session_id=session.id,
        run_id=session.active_run_id or "unknown",
        session_store=session.store,
        signal=signal,
        file_read_tracker=getattr(session, "file_read_tracker", None),
        deps=getattr(session, "run_deps", None),
        filesystem=getattr(session, "filesystem", None),
    )

    for attempt in range(max_attempts):
        if attempt > 0:
            throw_if_aborted(signal)
            delay_ms = _delay_for_error(last_exc or Exception(), attempt - 1, opts)  # type: ignore[arg-type]
            await asyncio.sleep(delay_ms / 1000.0)

        attempt_start = time.perf_counter()
        try:
            assert tool is not None
            effective_input = _effective_input(call, decision)
            coro = tool.execute(effective_input, ctx)
            if result_timeout_ms is None:
                result = await coro
            else:
                result = await asyncio.wait_for(coro, timeout=result_timeout_ms / 1000.0)
            elapsed = int((time.perf_counter() - attempt_start) * 1000)
            if result.duration_ms <= 0:
                result.duration_ms = elapsed
            # ── Auto-offload oversized results ────────────────────────────────
            block_result = await _maybe_offload_block(
                result, call=call, agent=agent, session=session
            )
            return _execution_outcome(call.id, result, block_result=block_result)
        except AbortError:
            raise
        except asyncio.TimeoutError:
            # result_timeout_ms may be None if the tool raised asyncio.TimeoutError
            # internally (its own deadline) with no agent-wide timeout set.
            ms: int | str = int(result_timeout_ms) if result_timeout_ms is not None else "unknown"
            te: Exception = ToolTimeoutError(f"Tool '{_tool_name(call)}' timed out after {ms}ms")
            last_exc = te
            if _tool_retryable(call, te) and attempt < max_attempts - 1:
                continue
            duration_ms = int((time.perf_counter() - started) * 1000)
            return _execution_outcome(
                call.id,
                _tool_result_error(
                    (
                        f"Tool '{_tool_name(call)}' timed out after {ms}ms"
                        " — retry with a larger timeout or narrower input."
                    ),
                    duration_ms,
                ),
            )
        except Exception as exc:
            last_exc = exc
            if _tool_retryable(call, exc) and attempt < max_attempts - 1:
                continue
            duration_ms = int((time.perf_counter() - started) * 1000)
            return _execution_outcome(
                call.id,
                _tool_result_error(f"Tool failed: {exc}", duration_ms),
            )

    # Defensive fallback — all attempts exhausted (should be unreachable because
    # the loop always returns or continues, but satisfies the type-checker).
    duration_ms = int((time.perf_counter() - started) * 1000)
    return _execution_outcome(
        call.id,
        _tool_result_error(
            (
                f"Tool '{_tool_name(call)}' failed after {max_attempts}"
                f" attempt{'s' if max_attempts != 1 else ''}: {last_exc}"
            ),
            duration_ms,
        ),
    )


def _tool_scope(call: ResolvedCall) -> str:
    return str(getattr(call.tool, "scope", "exec")) if call.tool is not None else "exec"


def _tool_parallel(call: ResolvedCall) -> bool:
    if call.is_immediate_error or call.tool is None:
        return False
    if _tool_scope(call) != "read":
        return False
    if hasattr(call.tool, "parallel"):
        return bool(getattr(call.tool, "parallel", False))
    return bool(getattr(call.tool, "parallel_safe", False))


def _resource_accesses(
    call: ResolvedCall,
    input: dict[str, Any] | None = None,
) -> list[ResourceAccess]:
    if call.is_immediate_error or call.tool is None:
        return []
    effective_input = input if input is not None else call.input
    resources = getattr(call.tool, "resources", None)
    if callable(resources):
        try:
            raw = resources(effective_input)
        except Exception:
            if _tool_scope(call) == "read":
                return []
            return [ResourceAccess(resource=f"tool:{_tool_name(call)}", mode="write")]
        if raw is None:
            return []
        if isinstance(raw, ResourceAccess):
            return [raw]
        result: list[ResourceAccess] = []
        if not isinstance(raw, list | tuple):
            return [ResourceAccess(resource=f"tool:{_tool_name(call)}", mode="write")]
        for item in raw:
            if isinstance(item, ResourceAccess):
                result.append(item)
            elif isinstance(item, dict):
                resource = item.get("resource")
                mode = item.get("mode", "read")
                if isinstance(resource, str) and mode in {"read", "write"}:
                    result.append(ResourceAccess(resource=resource, mode=mode))
        return result
    return []


def _resources_conflict(left: list[ResourceAccess], right: list[ResourceAccess]) -> bool:
    for a in left:
        for b in right:
            if a.resource == b.resource and (a.mode == "write" or b.mode == "write"):
                return True
    return False


def _max_concurrency(agent: Any) -> int:
    raw = getattr(agent, "max_tool_concurrency", None)
    if raw is None:
        raw = getattr(agent, "tool_concurrency", None)
    try:
        value = int(cast(Any, raw))
    except (TypeError, ValueError):
        value = 1
    return max(1, value)


def _tool_timeout_ms(agent: Any, tool: Any) -> float | None:
    """Resolve the execution timeout for a tool.

    Precedence: per-tool ``execution_timeout_ms`` class attribute
    → ``agent.tool_timeout_ms`` default → ``None`` (no timeout).

    A value of ``0`` or negative on the tool acts as an explicit opt-out
    even when an agent-wide default is set (e.g. Bash managing its own
    subprocess timeout).
    """
    if tool is not None:
        raw = getattr(tool, "execution_timeout_ms", None)
        if raw is not None:
            try:
                v = float(raw)
            except (TypeError, ValueError):
                v = 0.0
            return v if v > 0 else None  # 0 / negative = explicit opt-out
    raw = getattr(agent, "tool_timeout_ms", None)
    if raw is None:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _retry_options(agent: Any) -> RetryOptions | None:
    """Return ``RetryOptions`` from ``agent.tool_retry``, or ``None`` (no retry)."""
    val = getattr(agent, "tool_retry", None)
    if isinstance(val, RetryOptions):
        return val
    return None


def _tool_retryable(call: ResolvedCall, exc: Exception) -> bool:
    """True when the exception is safe to retry for this call.

    Read-scope tools are always retried on any exception — they are idempotent
    by design, so any failure is safe to retry.

    Write / exec tools are never retried by default (side-effect risk); a tool
    may opt in by setting a class-level ``retryable = True`` attribute, or an
    exception may carry ``retryable = True`` (e.g. ``ToolTimeoutError``).

    ``AbortError`` is never seen here — it is re-raised before the retry
    predicate is consulted.
    """
    tool = call.tool
    opt_in = bool(getattr(tool, "retryable", False)) if tool is not None else False
    if opt_in or _tool_scope(call) == "read":
        return True
    return bool(getattr(exc, "retryable", False))


def _scheduler_hooks(agent: Any) -> list[Any]:
    return list(getattr(agent, "hooks", []) or [])


async def _dispatch_pre_tool_use(
    dispatcher: HookDispatcher,
    call: ResolvedCall,
    input: dict[str, Any],
    session: Any,
    *,
    turn_index: int | None,
) -> tuple[dict[str, Any], str | None, list[Event]]:
    outcome = await dispatcher.dispatch(
        HookEvent.PRE_TOOL_USE,
        PreToolUseContext(
            session=session,
            run_id=session.active_run_id or "unknown",
            turn_index=turn_index,
            deps=getattr(session, "run_deps", None),
            tool_use_id=call.id,
            tool_name=_tool_name(call),
            input=input,
            summary=call.summary,
            tool=call.tool,
        ),
    )
    result = outcome.result
    if result.action == "mutate" and result.input is not None:
        return result.input, None, outcome.events
    if result.action in {"block", "stop"}:
        reason = result.reason or result.feedback or "Tool call blocked"
        return result.input or input, reason, outcome.events
    return input, None, outcome.events


async def _dispatch_post_tool_use(
    dispatcher: HookDispatcher,
    call: ResolvedCall,
    input: dict[str, Any],
    outcome: ToolExecutionOutcome,
    session: Any,
    *,
    agent: Any,
    turn_index: int | None,
) -> tuple[ToolExecutionOutcome, list[Event]]:
    dispatched = await dispatcher.dispatch(
        HookEvent.POST_TOOL_USE,
        PostToolUseContext(
            session=session,
            run_id=session.active_run_id or "unknown",
            turn_index=turn_index,
            deps=getattr(session, "run_deps", None),
            tool_use_id=call.id,
            tool_name=_tool_name(call),
            input=input,
            result=outcome.tool_result,
        ),
    )
    result = dispatched.result
    if result.action == "mutate" and result.tool_result is not None:
        mutated = result.tool_result
        # Re-run offload so a mutated oversized result doesn't bypass the
        # preview and re-inject the full payload into provider history.
        block_result = await _maybe_offload_block(mutated, call=call, agent=agent, session=session)
        return _execution_outcome(call.id, mutated, block_result=block_result), dispatched.events
    if result.action in {"block", "stop"}:
        blocked = _tool_result_error(
            result.reason or result.feedback or "Tool result blocked",
            outcome.duration_ms,
        )
        return _execution_outcome(call.id, blocked), dispatched.events
    return outcome, dispatched.events


def _partition_batches(
    resolved: list[ResolvedCall],
    decisions: list[PermissionDecision],
    *,
    max_concurrency: int,
) -> list[dict[str, Any]]:
    pending = [
        {
            "call": call,
            "idx": i,
            "decision": decisions[i],
            "resources": _resource_accesses(call, _effective_input(call, decisions[i])),
            "parallel": _tool_parallel(call),
        }
        for i, call in enumerate(resolved)
    ]
    batches: list[dict[str, Any]] = []

    while pending:
        first = pending[0]
        if not first["parallel"]:
            batches.append(
                {
                    "parallel": False,
                    "calls": [(first["call"], first["idx"], first["decision"])],
                }
            )
            pending = pending[1:]
            continue

        selected: list[dict[str, Any]] = []
        selected_resources: list[ResourceAccess] = []
        for item in pending:
            if len(selected) >= max_concurrency:
                break
            if not item["parallel"]:
                break
            resources = item["resources"]
            if _resources_conflict(selected_resources, resources):
                break
            selected.append(item)
            selected_resources.extend(resources)

        batches.append(
            {
                "parallel": len(selected) > 1,
                "calls": [(item["call"], item["idx"], item["decision"]) for item in selected],
            }
        )
        pending = pending[len(selected) :]
    return batches


async def execute_tool_calls(
    blocks: list[ToolUseBlock],
    agent: Any,
    session: Any,
    signal: AbortContext,
    *,
    turn_index: int | None = None,
) -> AsyncIterator[Event]:
    if not blocks:
        return

    effective_tools = getattr(session, "tools_override", None) or agent.tools
    resolved = [_resolve_call(b, effective_tools, agent.cwd) for b in blocks]
    hook_dispatcher = HookDispatcher(_scheduler_hooks(agent))

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
            # Seam A: replay a stored decision before falling through to the callback.
            _stored_decisions = getattr(session, "current_turn_permission_decisions", None)
            _key = _permission_key(_tool_name(call), call.input)
            if _stored_decisions is not None and _key in _stored_decisions:
                from .permissions.keys import permission_decision_from_dict as _pd_from_dict

                try:
                    decisions.append(_pd_from_dict(_stored_decisions[_key]))
                except (AttributeError, TypeError, ValueError):
                    ask_indices.append(i)
                    decisions.append(initial)
            else:
                allowed_tools = getattr(session, "current_turn_allowed_tools", None)
                if allowed_tools and tool_obj is not None and tool_obj.name in allowed_tools:
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
            # Seam B: persist allow + explicit user-deny so resume can replay.
            # Exception-path denials (network failure, abort) are NOT persisted.
            if decisions[idx].decision in ("allow", "deny"):
                _pd = getattr(session, "current_turn_permission_decisions", None)
                if _pd is not None:
                    from .permissions.keys import permission_decision_to_dict as _pd_to_dict

                    _pd[_permission_key(_tool_name(call), call.input)] = _pd_to_dict(decisions[idx])
        except AbortError:
            raise
        except Exception:
            decisions[idx] = PermissionDecision(
                decision="deny",
                reason=f"Permission resolution failed for {_tool_name(call)}",
            )

    # PreToolUse hooks can transform or block the permission-resolved input.
    middleware_errors: dict[str, str] = {}
    if hook_dispatcher.active:
        for i, call in enumerate(resolved):
            if call.is_immediate_error or decisions[i].decision != "allow":
                continue
            effective_input = _effective_input(call, decisions[i])
            updated_input, blocked_reason, hook_events = await _dispatch_pre_tool_use(
                hook_dispatcher,
                call,
                effective_input,
                session,
                turn_index=turn_index,
            )
            for hook_event in hook_events:
                yield hook_event
            decisions[i] = PermissionDecision(
                decision="allow",
                updated_input=updated_input,
            )
            try:
                if call.tool is not None:
                    call.summary = call.tool.summarize(updated_input)
            except Exception:
                pass
            if blocked_reason is not None:
                middleware_errors[call.id] = blocked_reason

    # Partition into bounded, resource-aware batches.
    batches = _partition_batches(
        resolved,
        decisions,
        max_concurrency=_max_concurrency(agent),
    )

    # Execute batches in order
    result_pairs: list[tuple[int, ToolResultBlock]] = []
    for batch in batches:
        throw_if_aborted(signal)

        if not batch["parallel"]:
            # Serial lane — one call at a time
            for call, idx, decision in batch["calls"]:
                input = _effective_input(call, decision)
                skill_name = _skill_name_from_input(call, input)
                if skill_name is not None:
                    args = input.get("args")
                    yield SkillInvokedEvent(
                        name=skill_name,
                        args=args if isinstance(args, str) else None,
                    )
                yield ToolCallStartEvent(
                    tool_use_id=call.id,
                    tool_name=_tool_name(call),
                    input=input,
                    summary=call.summary,
                )
                try:
                    outcome = await _execute_one(
                        call,
                        decision,
                        agent,
                        session,
                        signal,
                        turn_index=turn_index,
                        middleware_error=middleware_errors.get(call.id),
                    )
                    outcome, hook_events = await _dispatch_post_tool_use(
                        hook_dispatcher,
                        call,
                        input,
                        outcome,
                        session,
                        agent=agent,
                        turn_index=turn_index,
                    )
                    for hook_event in hook_events:
                        yield hook_event
                except AbortError:
                    tool_result = _tool_result_error("aborted")
                    yield ToolCallEndEvent(
                        tool_use_id=call.id,
                        tool_name=_tool_name(call),
                        result=tool_result.content,
                        is_error=tool_result.is_error,
                        duration_ms=tool_result.duration_ms,
                        tool_result=tool_result,
                    )
                    raise
                yield ToolCallEndEvent(
                    tool_use_id=call.id,
                    tool_name=_tool_name(call),
                    result=str(outcome.block.content),
                    is_error=outcome.block.is_error,
                    duration_ms=outcome.duration_ms,
                    tool_result=outcome.tool_result,
                )
                if skill_name is not None:
                    yield SkillCompletedEvent(name=skill_name, is_error=outcome.block.is_error)
                result_pairs.append((idx, outcome.block))
        else:
            # Parallel lane — all starts first, then run concurrently, then all ends
            skill_names: dict[str, str | None] = {}
            for call, _idx, decision in batch["calls"]:
                input = _effective_input(call, decision)
                skill_name = _skill_name_from_input(call, input)
                skill_names[call.id] = skill_name
                if skill_name is not None:
                    args = input.get("args")
                    yield SkillInvokedEvent(
                        name=skill_name,
                        args=args if isinstance(args, str) else None,
                    )
                yield ToolCallStartEvent(
                    tool_use_id=call.id,
                    tool_name=_tool_name(call),
                    input=input,
                    summary=call.summary,
                )

            tasks = [
                asyncio.ensure_future(
                    _execute_one(
                        call,
                        decision,
                        agent,
                        session,
                        signal,
                        turn_index=turn_index,
                        middleware_error=middleware_errors.get(call.id),
                    )
                )
                for call, _idx, decision in batch["calls"]
            ]

            started_ids: set[str] = set()
            id_to_call: dict[str, ResolvedCall] = {}
            for call, *_ in batch["calls"]:
                started_ids.add(call.id)
                id_to_call[call.id] = call

            try:
                results = await asyncio.gather(*tasks)
            except (AbortError, asyncio.CancelledError):
                for t in tasks:
                    t.cancel()
                # Orphan-bracket synthesis
                finished_ids: set[str] = set()
                for t in tasks:
                    if t.done() and not t.cancelled():
                        try:
                            outcome = t.result()
                            finished_ids.add(outcome.block.tool_use_id)
                        except Exception:
                            pass
                for tid in started_ids:
                    if tid not in finished_ids:
                        orphan_call = id_to_call.get(tid)
                        tool_result = _tool_result_error("aborted")
                        yield ToolCallEndEvent(
                            tool_use_id=tid,
                            tool_name=(_tool_name(orphan_call) if orphan_call else "unknown"),
                            result=tool_result.content,
                            is_error=tool_result.is_error,
                            duration_ms=tool_result.duration_ms,
                            tool_result=tool_result,
                        )
                        sn = skill_names.get(tid)
                        if sn is not None:
                            yield SkillCompletedEvent(name=sn, is_error=True)
                raise

            for (call, idx, decision), outcome in zip(batch["calls"], results, strict=True):
                input = _effective_input(call, decision)
                outcome, hook_events = await _dispatch_post_tool_use(
                    hook_dispatcher,
                    call,
                    input,
                    outcome,
                    session,
                    agent=agent,
                    turn_index=turn_index,
                )
                for hook_event in hook_events:
                    yield hook_event
                yield ToolCallEndEvent(
                    tool_use_id=call.id,
                    tool_name=_tool_name(call),
                    result=str(outcome.block.content),
                    is_error=outcome.block.is_error,
                    duration_ms=outcome.duration_ms,
                    tool_result=outcome.tool_result,
                )
                sn = skill_names.get(call.id)
                if sn is not None:
                    yield SkillCompletedEvent(name=sn, is_error=outcome.block.is_error)
                result_pairs.append((idx, outcome.block))
