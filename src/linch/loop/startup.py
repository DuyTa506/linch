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

from ..errors import ConfigError
from ..events import (
    ErrorEvent,
    Event,
    SkillsLoadedEvent,
    SystemEvent,
    UserEvent,
    event_from_dict,
    event_to_dict,
)
from ..hooks import AgentStartContext, AgentStopContext, HookEvent
from ..hooks.contexts import _AgentResult
from ..model_calls import _context_fingerprint
from ..run_store import durable_call
from ..types import TextBlock, message_from_dict, message_to_dict
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
    listing = agent.skill_listing_text
    catalog = agent.context.get("request_catalog")
    if catalog is not None:
        listing = catalog.skill_listing(session, listing)
    if listing and not session.tools_override:
        from ..skills.system_reminder import wrap_in_system_reminder

        reminder = wrap_in_system_reminder(listing)
        message.content.insert(0, TextBlock(text=reminder))
    return message


async def _emit_prompt_block(ctx: RunStartupContext, block_reason: str) -> AsyncIterator[Event]:
    """Emit the pre-loop terminal path for a blocked user prompt."""
    runtime = ctx.runtime
    startup = ctx.checkpoint.extension_state.get("linch.startup")
    if not (startup and startup.get("blocked_error_emitted")):
        event: Event = ErrorEvent(
            error={"name": "HookBlockedPrompt", "message": block_reason, "retryable": False}
        )
        await _persist_event(runtime.session, runtime.run_id, event)
        if startup is not None:
            startup["blocked_error_emitted"] = True
        yield event
    runtime.sync_checkpoint_accounting()
    duration_ms = int((time.time() - runtime.started) * 1000)
    blocked_result = _AgentResult(
        run_id=runtime.run_id,
        session_id=runtime.session.id,
        subtype="error",
        stop_reason="error",
        total_usage=runtime.total,
        duration_ms=duration_ms,
    )
    ctx.checkpoint.extension_state.pop("linch.startup", None)
    async for event in _error_result_tail(
        runtime.session,
        runtime=runtime,
        run_id=runtime.run_id,
        total=runtime.total,
        duration_ms=duration_ms,
        running_cost=runtime.running_cost,
        usage_complete=runtime.ledger.usage_complete,
    ):
        yield event
    ctx.blocked = True
    await ctx.dispatch_lifecycle(
        HookEvent.AGENT_STOP,
        AgentStopContext(
            session=runtime.session,
            run_id=runtime.run_id,
            turn_index=None,
            deps=getattr(runtime.session, "run_deps", None),
            result=runtime.terminal_result or blocked_result,
        ),
    )


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

    if ctx.checkpoint.phase == "started":
        baseline = ctx.checkpoint.extension_state.get("linch.startup")
        prepared = baseline.get("prepared_message") if baseline else None
        if prepared is not None:
            user_message = message_from_dict(prepared)
            emit_skills = baseline["emit_skills"]
            hook_events = [event_from_dict(event) for event in baseline["hook_events"]]
            block_reason = baseline["block_reason"]
        else:
            if not (baseline and baseline.get("system_emitted")):
                event: Event = SystemEvent(
                    session_id=session.id,
                    run_id=runtime.run_id,
                    model=agent.model,
                    tools=sorted(tool.name for tool in agent.tools.list()),
                    permission_mode=agent.permission_engine.mode,
                    cwd=agent.cwd,
                )
                await _persist_event(session, runtime.run_id, event)
                if baseline and baseline.get("preparation_required"):
                    baseline["system_emitted"] = True
                yield event

            emit_skills = not session.skills_loaded_emitted and bool(agent.skills)
            if emit_skills and not (baseline and baseline.get("skills_emitted")):
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
                if baseline and baseline.get("preparation_required"):
                    baseline["skills_emitted"] = True
                yield event

            prompt, images, hook_events, block_reason = await ctx.dispatch_user_prompt(
                prompt, ctx.opts.images, "run"
            )
            user_message = _prompt_message(ctx, prompt, images)
            if baseline and baseline.get("preparation_required"):
                baseline.update(
                    {
                        "history_count": session.session_log.history_count,
                        "history_fingerprint": session.session_log._history_fingerprint(),
                        "prepared_message": message_to_dict(user_message),
                        "emit_skills": emit_skills,
                        "hook_events": [event_to_dict(event) for event in hook_events],
                        "hook_event_cursor": 0,
                        "block_reason": block_reason,
                    }
                )
                await ctx.save_checkpoint("started")
        cursor = baseline.get("hook_event_cursor", 0) if baseline else 0
        for index, hook_event in enumerate(hook_events[cursor:], start=cursor):
            await _persist_event(session, runtime.run_id, hook_event)
            if baseline and baseline.get("preparation_required"):
                baseline["hook_event_cursor"] = index + 1
            yield hook_event
        if block_reason is not None:
            async for event in _emit_prompt_block(ctx, block_reason):
                if event.type == "result" and (
                    agent.run_store is None or session._event_journal is None
                ):
                    session.skills_loaded_emitted |= emit_skills
                yield event
            return
        append = ctx.resume_checkpoint is None or not _last_message_matches(session, user_message)
        if baseline is not None and ctx.resume_checkpoint is not None:
            count = baseline["history_count"]
            fingerprint = baseline["history_fingerprint"]
            current = session.session_log.history_count
            if current == count and session.session_log._history_fingerprint() == fingerprint:
                append = True
            elif not (
                current == count + 1
                and not append
                and _context_fingerprint(session.full_history[:count]) == fingerprint
            ):
                raise ConfigError("startup durable message baseline does not match")
        if append:
            await durable_call(session, "session.append", session.append([user_message]))
        event = UserEvent(message=user_message, subtype="prompt")
        await _persist_event(session, runtime.run_id, event)
        ctx.checkpoint.extension_state.pop("linch.startup", None)
        await ctx.save_checkpoint("user_appended")
        if emit_skills:
            session.skills_loaded_emitted = True
        yield event
