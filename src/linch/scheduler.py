from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from typing import Any, cast
from uuid import uuid4
from xml.sax.saxutils import escape

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
    PostToolUseFailureContext,
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


def _effective_cwd(session: Any, agent: Any) -> str:
    """Session cwd override (set by an isolation backend) or the agent's cwd."""
    return getattr(session, "cwd_override", None) or agent.cwd


def _background_ack(tool_name: str, bg_id: str) -> ToolResult:
    return ToolResult(
        content=(
            f"Tool '{tool_name}' started in background as '{bg_id}'."
            " You will receive a <task-notification> when it finishes."
        )
    )


async def _run_background_tool(
    call: ResolvedCall,
    decision: PermissionDecision,
    agent: Any,
    session: Any,
    signal: AbortContext,
    *,
    bg_id: str,
    turn_index: int | None,
    middleware_error: str | None,
) -> None:
    """Run a detached tool call and post its completion as a <task-notification>.

    Mirrors the background-subagent path: the result lands in
    ``session.pending_notifications`` (drained next turn) rather than the current
    turn's tool-result block. Cancellation (``session.abort()``) propagates as
    ``CancelledError`` and writes nothing into the dead session.
    """
    tool_name = _tool_name(call)
    try:
        outcome = await _execute_one(
            call,
            decision,
            agent,
            session,
            signal,
            turn_index=turn_index,
            middleware_error=middleware_error,
        )
    except (AbortError, asyncio.CancelledError):
        raise
    except Exception as exc:  # defensive: _execute_one normally returns error results
        result_text = f"{type(exc).__name__}: {exc}"
        is_error = True
    else:
        result_text = str(outcome.block.content)
        is_error = outcome.block.is_error

    notifications = getattr(session, "pending_notifications", None)
    if notifications is None:
        return
    status_str = "failed" if is_error else "completed"
    notification = (
        "<task-notification>"
        f"<task-id>{escape(bg_id)}</task-id>"
        f"<status>{status_str}</status>"
        f"<summary>Background tool '{escape(tool_name)}' finished.</summary>"
        f"<result>{escape(result_text)}</result>"
        "</task-notification>"
    )
    from .types import Message, TextBlock

    notifications.append(Message(role="user", content=[TextBlock(text=notification)]))
    emit_list = getattr(session, "pending_child_events", None)
    if emit_list is not None:
        from .events import BackgroundWorkerEvent

        emit_list.append(
            BackgroundWorkerEvent(worker_id=bg_id, status=status_str, display_name=tool_name)
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

    # A PreToolUse hook short-circuited this call (e.g. cache hit): use the
    # supplied result instead of executing. Re-run offload so an oversized
    # served result still gets a preview rather than re-injecting the full body.
    precomputed = getattr(decision, "precomputed_result", None)
    if precomputed is not None:
        block_result = await _maybe_offload_block(
            precomputed, call=call, agent=agent, session=session
        )
        return _execution_outcome(call.id, precomputed, block_result=block_result)

    tool = call.tool
    result_timeout_ms = _tool_timeout_ms(agent, tool)
    opts = _retry_options(agent)
    max_attempts = opts.max_attempts if opts is not None else 1
    started = time.perf_counter()
    last_exc: Exception | None = None

    ctx = ToolContext(
        cwd=_effective_cwd(session, agent),
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


def _tool_parallel(call: ResolvedCall, input: dict[str, Any] | None = None) -> bool:
    if call.is_immediate_error or call.tool is None:
        return False
    parallel = getattr(call.tool, "parallel", None)
    if callable(parallel):
        # Input-aware seam: the tool decides concurrency-safety per call, for any
        # scope (not just read). Fail closed — a misbehaving predicate serializes.
        try:
            return bool(parallel(input if input is not None else call.input))
        except Exception:
            return False
    if _tool_scope(call) != "read":
        return False
    if parallel is not None:
        return bool(parallel)
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
) -> tuple[dict[str, Any], str | None, Any, list[Event]]:
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
    if result.action == "resolve" and result.tool_result is not None:
        # A hook served a result (e.g. cache hit): skip execution, use it as-is.
        return input, None, result.tool_result, outcome.events
    if result.action == "mutate" and result.input is not None:
        return result.input, None, None, outcome.events
    if result.action in {"block", "stop"}:
        reason = result.reason or result.feedback or "Tool call blocked"
        return result.input or input, reason, None, outcome.events
    return input, None, None, outcome.events


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
    events = list(dispatched.events)
    if result.action == "mutate" and result.tool_result is not None:
        mutated = result.tool_result
        # Re-run offload so a mutated oversized result doesn't bypass the
        # preview and re-inject the full payload into provider history.
        block_result = await _maybe_offload_block(mutated, call=call, agent=agent, session=session)
        final = _execution_outcome(call.id, mutated, block_result=block_result)
    elif result.action in {"block", "stop"}:
        blocked = _tool_result_error(
            result.reason or result.feedback or "Tool result blocked",
            outcome.duration_ms,
        )
        final = _execution_outcome(call.id, blocked)
    else:
        final = outcome
    # PostToolUseFailure: an observational notification fired only when the final
    # (post-mutation) result is an error, so a failure-watcher hook need not
    # re-derive "did this fail?" from every PostToolUse.
    events.extend(
        await _dispatch_post_tool_use_failure(
            dispatcher, call, input, final, session, turn_index=turn_index
        )
    )
    return final, events


async def _dispatch_post_tool_use_failure(
    dispatcher: HookDispatcher,
    call: ResolvedCall,
    input: dict[str, Any],
    outcome: ToolExecutionOutcome,
    session: Any,
    *,
    turn_index: int | None,
) -> list[Event]:
    tool_result = outcome.tool_result
    if not dispatcher.active or tool_result is None or not getattr(tool_result, "is_error", False):
        return []
    dispatched = await dispatcher.dispatch(
        HookEvent.POST_TOOL_USE_FAILURE,
        PostToolUseFailureContext(
            session=session,
            run_id=session.active_run_id or "unknown",
            turn_index=turn_index,
            deps=getattr(session, "run_deps", None),
            tool_use_id=call.id,
            tool_name=_tool_name(call),
            input=input,
            result=tool_result,
        ),
    )
    return dispatched.events


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
            "parallel": _tool_parallel(call, _effective_input(call, decisions[i])),
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


def _strip_background_hints(
    blocks: list[ToolUseBlock], agent: Any
) -> tuple[list[ToolUseBlock], dict[str, str]]:
    """Pull the ``run_in_background`` hint off blocks when background-any-tool is
    enabled, returning the cleaned blocks and a ``{block_id: bg_id}`` map.

    Opt-in (``Agent(enable_background_tools=True)``); otherwise blocks pass
    through untouched and the map is empty so any tool can be backgrounded
    without declaring the key in its schema."""
    if not getattr(agent, "enable_background_tools", False):
        return blocks, {}
    bg_ids: dict[str, str] = {}
    stripped: list[ToolUseBlock] = []
    for b in blocks:
        inp = b.input
        if isinstance(inp, dict) and inp.get("run_in_background"):
            bg_ids[b.id] = f"bgtool_{uuid4().hex[:8]}"
            stripped.append(
                replace(b, input={k: v for k, v in inp.items() if k != "run_in_background"})
            )
        else:
            stripped.append(b)
    return stripped, bg_ids


def _evaluate_permissions(
    resolved: list[ResolvedCall], agent: Any, session: Any
) -> tuple[list[PermissionDecision], list[int]]:
    """First (synchronous) permission pass.

    Returns the per-call decisions plus the indices still needing an async
    ``resolve()`` (the ``ask`` calls). Handles immediate errors, Seam-A
    stored-decision replay, and the per-turn allowed-tools allowlist."""
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
                cwd=_effective_cwd(session, agent),
            )
        )
        if initial.decision != "ask":
            decisions.append(initial)
            continue

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
    return decisions, ask_indices


async def _resolve_ask_decisions(
    resolved: list[ResolvedCall],
    ask_indices: list[int],
    decisions: list[PermissionDecision],
    agent: Any,
    session: Any,
    signal: AbortContext,
) -> None:
    """Second (async) permission pass: drive ``resolve()`` for each ``ask`` call,
    mutating *decisions* in place.

    Persists allow + explicit user-deny outcomes so resume can replay them
    (Seam B); exception-path denials (network failure, abort) are NOT persisted."""
    for idx in ask_indices:
        call = resolved[idx]
        try:
            decisions[idx] = await agent.permission_engine.resolve(
                PendingToolCall(
                    tool_use_id=call.id,
                    tool=call.tool,
                    input=call.input,
                    cwd=_effective_cwd(session, agent),
                ),
                signal,
            )
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


async def _run_serial_batch(
    batch: Any,
    *,
    agent: Any,
    session: Any,
    signal: AbortContext,
    hook_dispatcher: HookDispatcher,
    middleware_errors: dict[str, str],
    turn_index: int | None,
) -> AsyncIterator[Event]:
    """Run a serial (one-call-at-a-time) batch, yielding skill + start/end events."""
    for call, _idx, decision in batch["calls"]:
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


async def _run_parallel_batch(
    batch: Any,
    *,
    agent: Any,
    session: Any,
    signal: AbortContext,
    hook_dispatcher: HookDispatcher,
    middleware_errors: dict[str, str],
    turn_index: int | None,
) -> AsyncIterator[Event]:
    """Run a parallel batch: emit all starts, gather concurrently, then all ends.

    On abort/cancel, synthesises an ``aborted`` end event for every call that
    started but never produced a result (orphan-bracket synthesis)."""
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

    for (call, _idx, decision), outcome in zip(batch["calls"], results, strict=True):
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

    blocks, bg_ids = _strip_background_hints(blocks, agent)

    effective_tools = getattr(session, "tools_override", None) or agent.tools
    resolved = [_resolve_call(b, effective_tools, agent.cwd) for b in blocks]
    hook_dispatcher = HookDispatcher(_scheduler_hooks(agent))

    # First pass: synchronous evaluate()
    decisions, ask_indices = _evaluate_permissions(resolved, agent, session)

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
    await _resolve_ask_decisions(resolved, ask_indices, decisions, agent, session, signal)

    # PreToolUse hooks can transform or block the permission-resolved input.
    middleware_errors: dict[str, str] = {}
    if hook_dispatcher.active:
        for i, call in enumerate(resolved):
            if call.is_immediate_error or decisions[i].decision != "allow":
                continue
            effective_input = _effective_input(call, decisions[i])
            (
                updated_input,
                blocked_reason,
                precomputed_result,
                hook_events,
            ) = await _dispatch_pre_tool_use(
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
                precomputed_result=precomputed_result,
            )
            try:
                if call.tool is not None:
                    call.summary = call.tool.summarize(updated_input)
            except Exception:
                pass
            if blocked_reason is not None:
                middleware_errors[call.id] = blocked_reason

    # Dispatch backgrounded tool calls: detach them, return an immediate ack as
    # their tool result, and exclude them from the foreground batches. Denied /
    # errored / hook-blocked calls fall through to normal foreground handling so
    # their error result still surfaces.
    background_indices: set[int] = set()
    if bg_ids:
        for i, call in enumerate(resolved):
            if (
                call.is_immediate_error
                or decisions[i].decision != "allow"
                or call.id not in bg_ids
                or call.id in middleware_errors
            ):
                continue
            background_indices.add(i)
            bg_id = bg_ids[call.id]
            tool_name = _tool_name(call)
            input = _effective_input(call, decisions[i])
            yield ToolCallStartEvent(
                tool_use_id=call.id,
                tool_name=tool_name,
                input=input,
                summary=call.summary,
            )
            task = asyncio.ensure_future(
                _run_background_tool(
                    call,
                    decisions[i],
                    agent,
                    session,
                    signal,
                    bg_id=bg_id,
                    turn_index=turn_index,
                    middleware_error=middleware_errors.get(call.id),
                )
            )
            session.background_tasks.append(task)
            ack = _background_ack(tool_name, bg_id)
            yield ToolCallEndEvent(
                tool_use_id=call.id,
                tool_name=tool_name,
                result=ack.content,
                is_error=False,
                duration_ms=0,
                tool_result=ack,
            )

    # Partition into bounded, resource-aware batches (foreground calls only).
    if background_indices:
        fg = [
            (resolved[i], decisions[i]) for i in range(len(resolved)) if i not in background_indices
        ]
        fg_resolved = [c for c, _ in fg]
        fg_decisions = [d for _, d in fg]
    else:
        fg_resolved, fg_decisions = resolved, decisions
    batches = _partition_batches(
        fg_resolved,
        fg_decisions,
        max_concurrency=_max_concurrency(agent),
    )

    # Execute batches in order; each batch runs on its serial or parallel lane.
    for batch in batches:
        throw_if_aborted(signal)
        lane = _run_serial_batch if not batch["parallel"] else _run_parallel_batch
        async for event in lane(
            batch,
            agent=agent,
            session=session,
            signal=signal,
            hook_dispatcher=hook_dispatcher,
            middleware_errors=middleware_errors,
            turn_index=turn_index,
        ):
            yield event
