"""Tool-batch phase for the agent run loop.

The provider phase hands an accepted tool-use assembly to this module.  The
batch owns recovery, durable tool execution checkpoints, tool event
projection, result-message persistence, and loop-guard evaluation.  The
runner remains responsible for the turn boundary around the batch.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any

from .. import lifecycle
from ..events import (
    Event,
    LoopGuardEvent,
    PermissionRequestEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolProgressEvent,
    UserEvent,
)
from ..hooks import ToolUseStartContext, ToolUseStopContext
from ..loop_guard import evaluate_loop_guard
from ..scheduler import execute_tool_calls
from ..types import (
    AssistantAssembly,
    ContentBlock,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from .checkpoint import (
    _interrupted_tool_result_block,
    _last_message_has_tool_results,
    _loop_guard_is_active,
    _recover_completed_tool_results,
    _replayed_feedback_present,
    _tool_result_block_from_end,
)


@dataclass(slots=True)
class ToolBatchContext:
    """Run-local dependencies required by :func:`run_tool_batch`."""

    session: Any
    agent: Any
    config: Any
    signal: Any
    run_id: str
    turn_index: int
    assembly: AssistantAssembly
    checkpoint: Any
    resume_checkpoint: Any
    stored_events: list[Any]
    guard: Any
    guard_state: Any
    save_checkpoint: Callable[..., Awaitable[None]]
    flush_events: Callable[[], Awaitable[None]]
    persist_event: Callable[[Event], Awaitable[None]]
    dispatch_lifecycle: Callable[..., Awaitable[None]]
    emit_error_terminal: Callable[..., AsyncIterator[Event]]
    observe_results: Callable[[list[ToolUseBlock], dict[str, ToolResultBlock]], None] | None = None
    force_final_pending: bool = False
    terminal: bool = False


async def run_tool_batch(ctx: ToolBatchContext) -> AsyncIterator[Event]:
    """Execute one assistant tool-use batch and yield its durable projection."""
    session = ctx.session
    resuming_turn = (
        ctx.resume_checkpoint is not None and ctx.turn_index == ctx.checkpoint.turn_index
    )
    replaying_results_checkpoint = resuming_turn and ctx.checkpoint.phase == "tool_results_appended"
    tool_blocks = [
        block for block in ctx.assembly.message.content if isinstance(block, ToolUseBlock)
    ]
    completed_tool_results: dict[str, ToolResultBlock] = (
        await _recover_completed_tool_results(
            session,
            ctx.run_id,
            ctx.checkpoint.completed_tool_results,
            tool_blocks,
            after_seq=ctx.checkpoint.tool_batch_event_after_seq,
        )
        if resuming_turn
        else {}
    )
    background_calls = (
        {
            (row.event.tool_use_id, row.event.tool_name)
            for row in ctx.stored_events
            if row.seq > (ctx.checkpoint.tool_batch_event_after_seq or 0)
            and isinstance(row.event, ToolCallStartEvent)
            and row.event.input.get("run_in_background")
        }
        if ctx.observe_results is not None
        else None
    )
    recovery_hints: dict[str, str] = {}
    missing_tool_blocks = [block for block in tool_blocks if block.id not in completed_tool_results]
    if not (replaying_results_checkpoint and not missing_tool_blocks):
        await ctx.save_checkpoint(
            "tool_batch_pending",
            turn_index=ctx.turn_index,
            assistant_message=ctx.assembly.message,
            assistant_stop_reason=ctx.assembly.stop_reason,
            pending_tool_blocks=tool_blocks,
            completed_tool_results=completed_tool_results,
        )

    async def checkpoint_tool_executing() -> None:
        """Persist the resolved permission decisions before any tool starts."""
        await ctx.save_checkpoint(
            "tool_executing",
            turn_index=ctx.turn_index,
            assistant_message=ctx.assembly.message,
            assistant_stop_reason=ctx.assembly.stop_reason,
            pending_tool_blocks=tool_blocks,
            completed_tool_results=completed_tool_results,
        )

    async with aclosing(
        execute_tool_calls(
            missing_tool_blocks,
            ctx.agent,
            session,
            ctx.signal,
            run_config=ctx.config,
            turn_index=ctx.turn_index,
            on_permissions_resolved=checkpoint_tool_executing,
        )
    ) as tool_events:
        async for event in tool_events:
            # Progress is intentionally transient.  Recovery-critical start/end
            # events are persisted and flushed before crossing the yield boundary.
            if not isinstance(event, ToolProgressEvent):
                await ctx.persist_event(event)
            if isinstance(event, (ToolCallStartEvent, ToolCallEndEvent)):
                await ctx.flush_events()
            if isinstance(event, PermissionRequestEvent):
                await ctx.save_checkpoint(
                    "permission_pending",
                    turn_index=ctx.turn_index,
                    assistant_message=ctx.assembly.message,
                    assistant_stop_reason=ctx.assembly.stop_reason,
                    pending_tool_blocks=tool_blocks,
                    completed_tool_results=completed_tool_results,
                    status="waiting_permission",
                )
            elif isinstance(event, ToolCallStartEvent):
                if background_calls is not None and event.input.get("run_in_background"):
                    background_calls.add((event.tool_use_id, event.tool_name))
                # A started-but-unfinished tool is recovered as interrupted from
                # the durable event log if a consumer crashes at the yield below.
                completed_tool_results.setdefault(
                    event.tool_use_id,
                    _interrupted_tool_result_block(event),
                )
            elif isinstance(event, ToolCallEndEvent):
                block = _tool_result_block_from_end(event)
                completed_tool_results[event.tool_use_id] = block
                if (
                    event.is_error
                    and event.tool_result is not None
                    and event.tool_result.recovery_hint
                ):
                    recovery_hints[event.tool_use_id] = event.tool_result.recovery_hint
            yield event
            if isinstance(event, ToolCallStartEvent):
                await ctx.dispatch_lifecycle(
                    lifecycle.TOOL_USE_START,
                    ToolUseStartContext(
                        session=session,
                        run_id=ctx.run_id,
                        turn_index=ctx.turn_index,
                        deps=getattr(session, "run_deps", None),
                        tool_use_id=event.tool_use_id,
                        tool_name=event.tool_name,
                        input=event.input,
                        summary=event.summary,
                    ),
                )
            elif isinstance(event, ToolCallEndEvent):
                await ctx.dispatch_lifecycle(
                    lifecycle.TOOL_USE_STOP,
                    ToolUseStopContext(
                        session=session,
                        run_id=ctx.run_id,
                        turn_index=ctx.turn_index,
                        deps=getattr(session, "run_deps", None),
                        tool_use_id=event.tool_use_id,
                        tool_name=event.tool_name,
                        is_error=event.is_error,
                        duration_ms=event.duration_ms,
                        result=event.result,
                        tool_result=event.tool_result,
                    ),
                )

    result_blocks: list[ContentBlock] = [completed_tool_results[block.id] for block in tool_blocks]
    result_message = Message(role="user", content=result_blocks)
    already_appended = resuming_turn and (
        replaying_results_checkpoint or _last_message_has_tool_results(session, tool_blocks)
    )
    if not already_appended:
        await session.append([result_message])

    if ctx.observe_results is not None and background_calls is not None:
        ctx.observe_results(
            [block for block in tool_blocks if (block.id, block.name) not in background_calls],
            completed_tool_results,
        )
    await ctx.save_checkpoint(
        "tool_results_appended",
        turn_index=ctx.turn_index,
        assistant_message=ctx.assembly.message,
        assistant_stop_reason=ctx.assembly.stop_reason,
        pending_tool_blocks=tool_blocks,
        completed_tool_results=completed_tool_results,
    )
    event = UserEvent(message=result_message, subtype="tool_result")
    await ctx.persist_event(event)
    yield event

    hint_event = await _append_recovery_hint(ctx, result_blocks, recovery_hints)
    if hint_event is not None:
        yield hint_event

    guard_decision = None
    if _loop_guard_is_active(ctx.guard) and ctx.guard_state is not None:
        guard_decision = evaluate_loop_guard(ctx.guard, ctx.guard_state, tool_blocks, result_blocks)

    if guard_decision is not None and guard_decision.action != "continue":
        event = LoopGuardEvent(
            reason=guard_decision.reason,
            detail=guard_decision.detail,
            action=guard_decision.action,
        )
        await ctx.persist_event(event)
        yield event
        if guard_decision.action == "force_final":
            reminder_event = await _inject_force_final_reminder(ctx)
            yield reminder_event
        else:
            async with aclosing(ctx.emit_error_terminal()) as error_events:
                async for error_event in error_events:
                    yield error_event
            ctx.terminal = True


async def _append_recovery_hint(
    ctx: ToolBatchContext,
    result_blocks: list[ContentBlock],
    recovery_hints: dict[str, str],
) -> Event | None:
    """Append the all-tools-failed recovery hint after the result message."""
    if not (
        result_blocks
        and all(
            getattr(block, "type", None) == "tool_result" and getattr(block, "is_error", False)
            for block in result_blocks
        )
        and recovery_hints
    ):
        return None
    hint_text = "All tool calls failed. Recovery hints:\n" + "\n".join(
        f"- {hint}" for hint in recovery_hints.values()
    )
    hint_message = Message(role="user", content=[TextBlock(text=hint_text)])
    await ctx.session.append([hint_message])
    hint_event: Event = UserEvent(message=hint_message, subtype="notification")
    await ctx.persist_event(hint_event)
    return hint_event


async def _inject_force_final_reminder(ctx: ToolBatchContext) -> Event:
    """Inject the loop-guard reminder and mark the next turn as final-only."""
    from ..skills.system_reminder import wrap_in_system_reminder

    reminder_text = wrap_in_system_reminder(
        "You appear to be stuck in a loop or repeatedly encountering failures. "
        "Please provide your final answer now without making further tool calls."
    )
    reminder_message = Message(role="user", content=[TextBlock(text=reminder_text)])
    already_appended = (
        ctx.resume_checkpoint is not None
        and ctx.turn_index == ctx.checkpoint.turn_index
        and ctx.checkpoint.phase == "tool_results_appended"
        and _replayed_feedback_present(
            ctx.session, (reminder_message,), {b.id for b in ctx.checkpoint.pending_tool_blocks}
        )
    )
    if not already_appended:
        await ctx.session.append([reminder_message])
    ctx.force_final_pending = True
    event: Event = UserEvent(message=reminder_message, subtype="notification")
    await ctx.persist_event(event)
    await ctx.save_checkpoint("turn_complete", turn_index=ctx.turn_index)
    return event
