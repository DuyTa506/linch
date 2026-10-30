"""Run setup and prompt persistence for the agent loop.

The startup phase owns the lifecycle event, initial system/skill events, and
the user prompt checkpoint.  Keeping this phase separate leaves the runner's
main generator responsible for turn choreography only.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .._telemetry import store_call
from ..events import ErrorEvent, Event, SkillsLoadedEvent, SystemEvent, UserEvent
from ..hooks import AgentStartContext, AgentStopContext, HookEvent
from ..observability import RunResultInfo
from ..types import TextBlock
from .checkpoint import _last_message_matches, _persist_event
from .request import build_user_message
from .terminals import _error_result_tail


@dataclass(slots=True)
class RunStartupContext:
    """Dependencies and mutable state needed by :func:`initialize_run`."""

    runtime: Any
    prompt: str
    opts: Any
    checkpoint: Any
    resume_checkpoint: Any
    save_checkpoint: Callable[..., Awaitable[None]]
    dispatch_user_prompt: Callable[..., Awaitable[Any]]
    dispatch_lifecycle: Callable[..., Awaitable[None]]
    blocked: bool = False


def _prompt_message(ctx: RunStartupContext, prompt: str, images: Any) -> Any:
    message = build_user_message(prompt, images)
    agent = ctx.runtime.agent
    session = ctx.runtime.session
    if agent.skill_listing_text and not session.tools_override:
        from ..skills.system_reminder import wrap_in_system_reminder

        reminder = wrap_in_system_reminder(agent.skill_listing_text)
        message.content.insert(0, TextBlock(text=reminder))
    return message


async def _emit_prompt_block(ctx: RunStartupContext, block_reason: str) -> AsyncIterator[Event]:
    """Emit the pre-loop terminal path for a blocked user prompt."""
    runtime = ctx.runtime
    runtime.sync_checkpoint_accounting()
    duration_ms = int((time.time() - runtime.started) * 1000)
    blocked_result = RunResultInfo(
        run_id=runtime.run_id,
        session_id=runtime.session.id,
        subtype="error",
        stop_reason="error",
        total_usage=runtime.total,
        duration_ms=duration_ms,
    )
    event: Event = ErrorEvent(
        error={"name": "HookBlockedPrompt", "message": block_reason, "retryable": False}
    )
    await _persist_event(runtime.session, runtime.run_id, event)
    yield event
    async for event in _error_result_tail(
        runtime.session,
        runtime.agent,
        run_id=runtime.run_id,
        run_record=runtime.run_record,
        checkpoint=runtime.checkpoint,
        total=runtime.total,
        duration_ms=duration_ms,
        running_cost=runtime.running_cost,
        usage_complete=runtime.ledger.usage_complete,
    ):
        yield event
    await ctx.dispatch_lifecycle(
        HookEvent.AGENT_STOP,
        AgentStopContext(
            session=runtime.session,
            run_id=runtime.run_id,
            turn_index=None,
            deps=getattr(runtime.session, "run_deps", None),
            result=blocked_result,
        ),
    )
    ctx.blocked = True


async def initialize_run(ctx: RunStartupContext) -> AsyncIterator[Event]:
    """Persist run startup state and the initial user prompt."""
    runtime = ctx.runtime
    session = runtime.session
    agent = runtime.agent
    prompt = ctx.prompt

    await ctx.dispatch_lifecycle(
        HookEvent.AGENT_START,
        AgentStartContext(
            session=session,
            run_id=runtime.run_id,
            turn_index=None,
            deps=getattr(session, "run_deps", None),
            model=agent.model,
            prompt=prompt,
            tools=tuple(sorted(tool.name for tool in agent.tools.list())),
        ),
    )

    if ctx.resume_checkpoint is None:
        await ctx.save_checkpoint("started")
        event: Event = SystemEvent(
            session_id=session.id,
            run_id=runtime.run_id,
            model=agent.model,
            tools=sorted(tool.name for tool in agent.tools.list()),
            permission_mode=agent.permission_engine.mode,
            cwd=agent.cwd,
        )
        await _persist_event(session, runtime.run_id, event)
        yield event

        if not session.skills_loaded_emitted and agent.skills:
            session.skills_loaded_emitted = True
            skills_data = [
                {
                    "name": skill.name,
                    "description": skill.frontmatter.description,
                    **(
                        {"when_to_use": skill.frontmatter.when_to_use}
                        if skill.frontmatter.when_to_use
                        else {}
                    ),
                    **(
                        {"argument_hint": skill.frontmatter.argument_hint}
                        if skill.frontmatter.argument_hint
                        else {}
                    ),
                }
                for skill in sorted(agent.skills.values(), key=lambda item: item.name)
            ]
            event = SkillsLoadedEvent(skills=skills_data)
            await _persist_event(session, runtime.run_id, event)
            yield event

        prompt, images, hook_events, block_reason = await ctx.dispatch_user_prompt(
            prompt, ctx.opts.images, "run"
        )
        for hook_event in hook_events:
            await _persist_event(session, runtime.run_id, hook_event)
            yield hook_event
        if block_reason is not None:
            async for event in _emit_prompt_block(ctx, block_reason):
                yield event
            return
        user_message = _prompt_message(ctx, prompt, images)
        await store_call(session, "session.append", session.append([user_message]))
        await ctx.save_checkpoint("user_appended")
        event = UserEvent(message=user_message, subtype="prompt")
        await _persist_event(session, runtime.run_id, event)
        yield event
    elif ctx.checkpoint.phase == "started":
        prompt, images, hook_events, block_reason = await ctx.dispatch_user_prompt(
            prompt, ctx.opts.images, "run"
        )
        for hook_event in hook_events:
            await _persist_event(session, runtime.run_id, hook_event)
            yield hook_event
        if block_reason is not None:
            async for event in _emit_prompt_block(ctx, block_reason):
                yield event
            return
        user_message = _prompt_message(ctx, prompt, images)
        if not _last_message_matches(session, user_message):
            await store_call(session, "session.append", session.append([user_message]))
        await ctx.save_checkpoint("user_appended")
        event = UserEvent(message=user_message, subtype="prompt")
        await _persist_event(session, runtime.run_id, event)
        yield event
