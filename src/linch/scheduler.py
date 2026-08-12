from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any, Literal, cast
from uuid import uuid4
from xml.sax.saxutils import escape

from .abort import AbortContext, throw_if_aborted
from .errors import AbortError, ToolTimeoutError
from .events import (
    BackgroundWorkerEvent,
    Event,
    PermissionRequestEvent,
    PermissionRequestItem,
    SkillCompletedEvent,
    SkillInvokedEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolProgressEvent,
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

# How parallel-safe tool calls in one assistant turn are packed into batches.
# "greedy": break each batch at the first conflict/cap (contiguous prefix).
# "maximal": pack every currently-compatible call into each batch, skipping
# conflicts and admitting them in a later batch — non-parallel calls stay hard
# barriers. Result blocks/hooks remain provider-ordered either way.
ToolBatchingStrategy = Literal["greedy", "maximal"]


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


@dataclass(slots=True)
class _RunningTool:
    task: asyncio.Task[ToolExecutionOutcome]
    progress: asyncio.Queue[ToolProgressEvent]


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
    if not isinstance(validated, dict):
        return ResolvedCall(
            id=block.id,
            block=block,
            tool=tool,
            input=block.input,
            summary=f"{block.name}(invalid input)",
            is_immediate_error=True,
            immediate_error_reason="tool.validate() must return a dict",
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
    origin_run_id: str,
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
            run_id=origin_run_id,
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
    completion_event = BackgroundWorkerEvent(
        worker_id=bg_id,
        status=status_str,
        display_name=tool_name,
    )
    emit_list = getattr(session, "pending_child_events", None)
    if emit_list is not None:
        emit_list.append(completion_event)

    # Attribute detached completion to the run that launched it, even after
    # ``Session._iterate`` has cleared ``active_run_id``. This is best-effort:
    # notification delivery remains in-memory, but the typed completion record
    # survives in the existing run event journal when a RunStore is configured.
    run_store = getattr(agent, "run_store", None)
    if run_store is not None:
        try:
            await run_store.append_event(origin_run_id, completion_event)
        except Exception:
            pass


def _tool_name(call: ResolvedCall) -> str:
    return call.tool.name if call.tool else call.block.name


def _effective_input(call: ResolvedCall, decision: PermissionDecision) -> dict[str, Any]:
    # Canonical input is finalized before permission evaluation. Permission
    # decisions cannot mutate it in Linch 2.
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
    on_progress: Callable[[ToolProgressEvent], None] | None = None,
    run_id: str | None = None,
) -> ToolExecutionOutcome:
    throw_if_aborted(signal)

    if call.is_immediate_error:
        return _execution_outcome(
            call.id,
            _tool_result_error(call.immediate_error_reason or "unknown error"),
        )

    if middleware_error is not None:
        return _execution_outcome(call.id, _tool_result_error(middleware_error))

    if decision.decision == "deny":
        return _execution_outcome(
            call.id,
            _tool_result_error(f"Tool call denied: {decision.reason or 'permission denied'}"),
        )

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

    execution_run_id = run_id or session.active_run_id or "unknown"
    accepting_progress = True

    def _report_progress(message: str, data: dict[str, Any] | None) -> None:
        if not accepting_progress or on_progress is None:
            return
        on_progress(
            ToolProgressEvent(
                tool_use_id=call.id,
                tool_name=_tool_name(call),
                message=message,
                data=dict(data) if data is not None else None,
            )
        )

    ctx = ToolContext(
        cwd=_effective_cwd(session, agent),
        session_id=session.id,
        run_id=execution_run_id,
        session_store=session.store,
        signal=signal,
        file_read_tracker=getattr(session, "file_read_tracker", None),
        emit=_report_progress,
        deps=getattr(session, "run_deps", None),
        filesystem=getattr(session, "filesystem", None),
        # Stable across resume: run_id is reused by resume_loop and call.id comes
        # from the persisted provider_view, so a re-executed tool sees the same key.
        idempotency_key=f"{execution_run_id}:{call.id}",
    )
    try:
        return await _execute_tool_attempts(
            call,
            decision,
            agent,
            session,
            signal,
            ctx,
            tool,
            result_timeout_ms,
            opts,
            max_attempts,
            started,
        )
    finally:
        # A tool may retain ctx.report_progress and call it from a stray task.
        # Once execute() settles those late reports are stale and must be ignored.
        accepting_progress = False


async def _execute_tool_attempts(
    call: ResolvedCall,
    decision: PermissionDecision,
    agent: Any,
    session: Any,
    signal: AbortContext,
    ctx: ToolContext,
    tool: Any,
    result_timeout_ms: float | None,
    opts: RetryOptions | None,
    max_attempts: int,
    started: float,
) -> ToolExecutionOutcome:
    last_exc: Exception | None = None
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
    # The fully mutated input (after every PreToolUse `mutate`). Report it on
    # every short-circuit path so the recorded decision / ToolCallStartEvent
    # match what actually ran or was served, even when a hook rewrote the input.
    final_input = getattr(outcome.context, "input", input)
    if result.action == "resolve":
        # A hook served a result (e.g. cache hit): skip execution, use it as-is.
        if result.tool_result is not None:
            return final_input, None, result.tool_result, outcome.events
        # Malformed resolve (no tool_result): the hook meant to short-circuit, so
        # block rather than silently running the tool it intended to suppress.
        reason = result.reason or result.feedback or "resolve hook returned no tool_result"
        return final_input, reason, None, outcome.events
    if result.action == "mutate" and result.input is not None:
        return result.input, None, None, outcome.events
    if result.action in {"block", "stop"}:
        reason = result.reason or result.feedback or "Tool call blocked"
        return final_input, reason, None, outcome.events
    return final_input, None, None, outcome.events


def _canonicalize_hook_input(call: ResolvedCall, input: Any) -> str | None:
    """Validate the final PreToolUse input and update the resolved call in place."""
    if call.tool is None:
        return call.immediate_error_reason or "tool is not registered"
    if not isinstance(input, dict):
        return "PreToolUse input must be a dict"
    try:
        validated = call.tool.validate(input)
    except Exception as exc:
        return f"PreToolUse produced invalid input: {exc}"
    if not isinstance(validated, dict):
        return "PreToolUse produced invalid input: tool.validate() must return a dict"
    call.input = validated
    try:
        call.summary = call.tool.summarize(validated)
    except Exception:
        call.summary = f"{_tool_name(call)}(...)"
    return None


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
    strategy: ToolBatchingStrategy = "greedy",
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
    if strategy == "maximal":
        return _partition_maximal(pending, max_concurrency=max_concurrency)
    return _partition_greedy(pending, max_concurrency=max_concurrency)


def _batch(selected: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "parallel": len(selected) > 1,
        "calls": [(item["call"], item["idx"], item["decision"]) for item in selected],
    }


def _partition_greedy(
    pending: list[dict[str, Any]], *, max_concurrency: int
) -> list[dict[str, Any]]:
    """Contiguous-prefix batching: each parallel batch stops at the first
    conflict, non-parallel call, or the concurrency cap."""
    batches: list[dict[str, Any]] = []
    while pending:
        first = pending[0]
        if not first["parallel"]:
            batches.append(_batch([first]))
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

        batches.append(_batch(selected))
        pending = pending[len(selected) :]
    return batches


def _partition_maximal(
    pending: list[dict[str, Any]], *, max_concurrency: int
) -> list[dict[str, Any]]:
    """Maximal packing: within each run of consecutive parallel-safe calls, admit
    every currently-compatible call (in provider order, up to the cap) into a
    batch, deferring conflicting calls to a later batch instead of ending the
    batch at the first conflict. Non-parallel calls stay hard barriers.

    Equivalent to greedy when no two parallel calls conflict; only conflicting
    inputs pack differently. Provider-order of result blocks is preserved by the
    runner's per-id reassembly, independent of admission order.
    """
    batches: list[dict[str, Any]] = []
    i = 0
    n = len(pending)
    while i < n:
        item = pending[i]
        if not item["parallel"]:
            batches.append(_batch([item]))
            i += 1
            continue

        # Consecutive run of parallel-safe calls, bounded by the next barrier.
        j = i
        while j < n and pending[j]["parallel"]:
            j += 1
        remaining = pending[i:j]
        i = j

        # Bin-pack the run: each pass admits non-conflicting calls in provider
        # order; the first call always fits (empty batch never conflicts and the
        # cap is >= 1), so remaining strictly shrinks.
        while remaining:
            selected: list[dict[str, Any]] = []
            selected_resources: list[ResourceAccess] = []
            leftover: list[dict[str, Any]] = []
            for member in remaining:
                if len(selected) >= max_concurrency or _resources_conflict(
                    selected_resources, member["resources"]
                ):
                    leftover.append(member)
                    continue
                selected.append(member)
                selected_resources.extend(member["resources"])
            batches.append(_batch(selected))
            remaining = leftover
    return batches


def _batching_strategy(agent: Any) -> ToolBatchingStrategy:
    """Resolve the tool-batching strategy off *agent*; unset/unknown → greedy."""
    return "maximal" if getattr(agent, "tool_batching_strategy", None) == "maximal" else "greedy"


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


def _enforce_turn_tool_availability(resolved: list[ResolvedCall], session: Any) -> None:
    """Fail closed when a call names a tool not offered on this turn.

    ``current_turn_allowed_tools`` is restored from skill overlays/checkpoints
    and also shapes the provider request. A provider can nevertheless emit an
    unoffered/global tool name, so execution must enforce the same availability
    boundary independently of provider compliance and permission mode.
    """
    allowed = getattr(session, "current_turn_allowed_tools", None)
    if allowed is None:
        return
    offered = {str(name) for name in allowed}
    for call in resolved:
        if call.is_immediate_error or _tool_name(call) in offered:
            continue
        call.is_immediate_error = True
        call.immediate_error_reason = (
            f"Tool '{_tool_name(call)}' was not offered for the current turn"
        )
        call.summary = f"{_tool_name(call)}(unavailable)"


def _evaluate_permissions(
    resolved: list[ResolvedCall],
    agent: Any,
    session: Any,
    *,
    middleware_errors: dict[str, str] | None = None,
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
        if middleware_errors and call.id in middleware_errors:
            decisions.append(PermissionDecision(decision="deny", reason=middleware_errors[call.id]))
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
            running = _start_tool_execution(
                call,
                decision,
                agent,
                session,
                signal,
                turn_index=turn_index,
                middleware_error=middleware_errors.get(call.id),
            )
            async for progress in _drain_tool_execution(running):
                yield progress
            outcome = running.task.result()
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

    running = [
        _start_tool_execution(
            call,
            decision,
            agent,
            session,
            signal,
            turn_index=turn_index,
            middleware_error=middleware_errors.get(call.id),
        )
        for call, _idx, decision in batch["calls"]
    ]

    emitted_ids: set[str] = set()
    try:
        for (call, _idx, decision), execution in zip(batch["calls"], running, strict=True):
            # While waiting for the next provider-ordered result, multiplex
            # every call's bounded progress queue. A later tool can therefore
            # update live without allowing its final end bracket to overtake.
            async for progress in _drain_parallel_progress_until(running, execution.task):
                yield progress
            outcome = execution.task.result()
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
            emitted_ids.add(call.id)
            sn = skill_names.get(call.id)
            if sn is not None:
                yield SkillCompletedEvent(name=sn, is_error=outcome.block.is_error)
    except (AbortError, asyncio.CancelledError):
        for execution in running:
            execution.task.cancel()
        # Await the cancelled tool tasks so their finalizers run before we
        # synthesize aborted ends and re-raise.
        await asyncio.gather(*(execution.task for execution in running), return_exceptions=True)
        # Orphan-bracket synthesis: every started call that has not already
        # emitted a real end gets exactly one synthetic aborted end, in provider
        # order, so cancellation still closes every start/end bracket.
        for call, _idx, _decision in batch["calls"]:
            if call.id in emitted_ids:
                continue
            tool_result = _tool_result_error("aborted")
            yield ToolCallEndEvent(
                tool_use_id=call.id,
                tool_name=_tool_name(call),
                result=tool_result.content,
                is_error=tool_result.is_error,
                duration_ms=tool_result.duration_ms,
                tool_result=tool_result,
            )
            sn = skill_names.get(call.id)
            if sn is not None:
                yield SkillCompletedEvent(name=sn, is_error=True)
        raise


def _start_tool_execution(
    call: ResolvedCall,
    decision: PermissionDecision,
    agent: Any,
    session: Any,
    signal: AbortContext,
    *,
    turn_index: int | None,
    middleware_error: str | None,
) -> _RunningTool:
    # One pending update per call. A fast producer coalesces to its latest
    # progress instead of creating an unbounded queue behind a slow consumer.
    progress: asyncio.Queue[ToolProgressEvent] = asyncio.Queue(maxsize=1)

    def _on_progress(event: ToolProgressEvent) -> None:
        if progress.full():
            try:
                progress.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            progress.put_nowait(event)
        except asyncio.QueueFull:
            pass

    task = asyncio.ensure_future(
        _execute_one(
            call,
            decision,
            agent,
            session,
            signal,
            turn_index=turn_index,
            middleware_error=middleware_error,
            on_progress=_on_progress,
        )
    )
    return _RunningTool(task=task, progress=progress)


async def _drain_tool_execution(running: _RunningTool) -> AsyncIterator[ToolProgressEvent]:
    """Yield bounded/coalesced progress live until the tool task settles."""
    while True:
        while not running.progress.empty():
            yield running.progress.get_nowait()
        if running.task.done():
            # A report can race with settlement; drain it before the end event.
            while not running.progress.empty():
                yield running.progress.get_nowait()
            return
        progress_wait = asyncio.ensure_future(running.progress.get())
        done, _ = await asyncio.wait(
            [running.task, progress_wait], return_when=asyncio.FIRST_COMPLETED
        )
        if progress_wait in done:
            yield progress_wait.result()
        else:
            progress_wait.cancel()
            try:
                await progress_wait
            except asyncio.CancelledError:
                pass


async def _drain_parallel_progress_until(
    executions: list[_RunningTool],
    until: asyncio.Task[ToolExecutionOutcome],
) -> AsyncIterator[ToolProgressEvent]:
    while True:
        for execution in executions:
            while not execution.progress.empty():
                yield execution.progress.get_nowait()
        if until.done():
            return

        unfinished = [execution for execution in executions if not execution.task.done()]

        queue_waiters = {
            asyncio.ensure_future(execution.progress.get()): execution for execution in unfinished
        }
        try:
            done, _ = await asyncio.wait(
                [*(execution.task for execution in unfinished), *queue_waiters],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for waiter in queue_waiters:
                if waiter in done:
                    yield waiter.result()
                else:
                    waiter.cancel()
        finally:
            for waiter in queue_waiters:
                if not waiter.done():
                    waiter.cancel()
            if queue_waiters:
                await asyncio.gather(*queue_waiters, return_exceptions=True)


async def execute_tool_calls(
    blocks: list[ToolUseBlock],
    agent: Any,
    session: Any,
    signal: AbortContext,
    *,
    turn_index: int | None = None,
    on_permissions_resolved: Callable[[], Awaitable[None]] | None = None,
) -> AsyncIterator[Event]:
    if not blocks:
        return

    blocks, bg_ids = _strip_background_hints(blocks, agent)

    effective_tools = getattr(session, "tools_override", None) or agent.tools
    resolved = [_resolve_call(b, effective_tools, agent.cwd) for b in blocks]
    _enforce_turn_tool_availability(resolved, session)
    hook_dispatcher = HookDispatcher(_scheduler_hooks(agent))

    # PreToolUse is the only input-mutation seam. It runs after the tool's
    # initial schema validation but before final policy evaluation. Every
    # mutation is validated again and becomes the canonical input used for
    # summary, resources, stored-decision key, callback, and execution.
    middleware_errors: dict[str, str] = {}
    precomputed_results: dict[str, Any] = {}
    if hook_dispatcher.active:
        for call in resolved:
            if call.is_immediate_error:
                continue
            (
                updated_input,
                blocked_reason,
                precomputed_result,
                hook_events,
            ) = await _dispatch_pre_tool_use(
                hook_dispatcher,
                call,
                call.input,
                session,
                turn_index=turn_index,
            )
            for hook_event in hook_events:
                yield hook_event
            invalid_reason = _canonicalize_hook_input(call, updated_input)
            if invalid_reason is not None:
                middleware_errors[call.id] = invalid_reason
            elif blocked_reason is not None:
                middleware_errors[call.id] = blocked_reason
            elif precomputed_result is not None:
                precomputed_results[call.id] = precomputed_result

    # Final permission evaluation binds to the canonical, post-hook input.
    decisions, ask_indices = _evaluate_permissions(
        resolved, agent, session, middleware_errors=middleware_errors
    )

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

    for i, call in enumerate(resolved):
        if decisions[i].decision == "allow" and call.id in precomputed_results:
            decisions[i].precomputed_result = precomputed_results[call.id]

    # Permissions are resolved and PreToolUse middleware has settled. Fire the
    # once-per-batch durability callback so the caller can persist the resolved
    # permission decisions and the tool_executing phase before any tool — the
    # foreground batches or the detached background calls below — runs. A crash
    # after this point recovers tool state from the durable event log instead of
    # a per-tool checkpoint.
    if on_permissions_resolved is not None:
        await on_permissions_resolved()

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
                    origin_run_id=session.active_run_id or "unknown",
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
        strategy=_batching_strategy(agent),
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
