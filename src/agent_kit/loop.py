from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from uuid import uuid4

from .compaction import build_compaction_event, maybe_compact, run_forced_compaction
from .errors import AbortError, ContextLengthError
from .events import (
    AssistantEvent,
    ErrorEvent,
    Event,
    PartialAssistantEvent,
    ResultEvent,
    SkillsLoadedEvent,
    SystemEvent,
    ToolCallEndEvent,
    UsageEvent,
    UserEvent,
)
from .scheduler import execute_tool_calls
from .session import RunOptions, Session
from .types import (
    AssistantAssembly,
    ImageBlock,
    Message,
    ProviderRequest,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)


def build_user_message(prompt: str, images: list[dict[str, str]] | None = None) -> Message:
    content = [
        TextBlock(text="<env>\nToday's date: " + time.strftime("%Y-%m-%d") + "\n</env>"),
        TextBlock(text=prompt),
    ]
    for image in images or []:
        if "url" in image:
            content.append(ImageBlock(source={"type": "url", "url": image["url"]}))
        else:
            content.append(
                ImageBlock(
                    source={
                        "type": "base64",
                        "media_type": image["media_type"],
                        "data": image["data"],
                    }
                )
            )
    return Message(role="user", content=content)


def final_text(message: Message) -> str | None:
    for block in message.content:
        if isinstance(block, TextBlock):
            return block.text
    return None


def _re_inject_skill_context(session: Session) -> None:
    agent = session.agent
    if not agent.skill_listing_text and not session.invoked_skills:
        return
    from .skills.system_reminder import wrap_in_system_reminder

    if agent.skill_listing_text:
        text = wrap_in_system_reminder(agent.skill_listing_text)
        session.provider_view.append(Message(role="user", content=[TextBlock(text=text)]))
    for rec in session.invoked_skills:
        text = wrap_in_system_reminder(
            f"Below is the body of a previously invoked skill "
            f"named '{rec.name}'.\n\n{rec.substituted_body}"
        )
        session.provider_view.append(Message(role="user", content=[TextBlock(text=text)]))


async def _run_context_injectors(
    session: Session,
    turn_index: int,
    extra_system: list,
) -> None:
    """Fire all registered context injectors for the current turn."""
    from .context_hooks import TurnContext

    agent = session.agent
    if not agent.context_injectors:
        return

    ctx = TurnContext(
        session=session,
        provider_view=session.provider_view,
        turn_index=turn_index,
        deps=getattr(session, "run_deps", None),
        extra_system=extra_system,
    )
    for injector in agent.context_injectors:
        await injector.before_turn(ctx)


def _build_turn_request(
    session: Session,
    opts: RunOptions,
    *,
    extra_system: list | None = None,
    model_override: str | None = None,
) -> ProviderRequest:
    """Build the :class:`ProviderRequest` for one provider call.

    Collapses the two near-identical request builders (normal path and
    ContextLengthError retry path) into one place.
    """
    agent = session.agent

    base_system = list(session.system_blocks_override or agent.system_blocks)
    if extra_system:
        base_system = base_system + list(extra_system)

    return ProviderRequest(
        model=model_override or agent.model,
        system=base_system,
        tools=(session.tools_override or agent.tools).schemas(),
        messages=session.provider_view,
        max_output_tokens=opts.max_output_tokens or agent.max_output_tokens,
        temperature=opts.temperature,
        thinking=opts.thinking,
        effort=opts.effort or None,
        output_schema=opts.output_schema or agent.output_schema,
        tool_choice=opts.tool_choice or agent.tool_choice,
        max_retries=agent.max_retries,
        cache_ttl=agent.cache_ttl,
        cache_prompt=True,
    )


def _parse_structured_output(text: str, schema: object) -> tuple[dict | None, str | None]:
    """Try to parse *text* as JSON and optionally validate against *schema*.

    Returns ``(parsed_dict, None)`` on success or ``(None, error_message)``
    on failure.
    """
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"JSON parse error: {exc}"

    if not isinstance(parsed, dict):
        return None, f"Expected a JSON object, got {type(parsed).__name__}"

    # Optionally validate against the JSON Schema (requires jsonschema extra).
    try:
        import jsonschema  # type: ignore[import]

        schema_dict = getattr(schema, "schema", None)
        if schema_dict:
            jsonschema.validate(parsed, schema_dict)
    except ImportError:
        pass  # jsonschema not installed — skip validation
    except Exception as exc:
        return None, f"Schema validation error: {exc}"

    return parsed, None


async def stream_turn(
    session: Session, req: ProviderRequest
) -> AsyncIterator[PartialAssistantEvent | AssistantAssembly]:
    agent = session.agent
    text_buf = ""
    thinking_buf = ""
    thinking_sig: str | None = None
    tool_inputs: dict[str, str] = {}
    tool_meta: dict[str, tuple[str, str]] = {}
    content: list[TextBlock | ThinkingBlock | ToolUseBlock] = []
    stop_reason = "end_turn"
    usage = Usage()
    metadata = None

    def flush_text() -> None:
        nonlocal text_buf
        if text_buf:
            content.append(TextBlock(text=text_buf))
            text_buf = ""

    def flush_thinking() -> None:
        nonlocal thinking_buf, thinking_sig
        if thinking_buf:
            content.append(ThinkingBlock(thinking=thinking_buf, signature=thinking_sig))
            thinking_buf = ""
            thinking_sig = None

    async for event in agent.provider.stream(req):
        typ = event["type"]
        if typ == "text_delta":
            flush_thinking()
            text_buf += event["text"]
            if agent.include_partial_messages:
                yield PartialAssistantEvent(delta={"kind": "text", "text": event["text"]})
        elif typ == "thinking_delta":
            flush_text()
            thinking_buf += event["text"]
            thinking_sig = event.get("signature", thinking_sig)
            if agent.include_partial_messages:
                yield PartialAssistantEvent(delta={"kind": "thinking", "text": event["text"]})
        elif typ == "tool_use_start":
            flush_text()
            flush_thinking()
            tool_meta[event["id"]] = (event["id"], event["name"])
            tool_inputs[event["id"]] = ""
        elif typ == "tool_use_input_delta":
            tool_inputs[event["id"]] = tool_inputs.get(event["id"], "") + event["json_delta"]
            if agent.include_partial_messages:
                yield PartialAssistantEvent(
                    delta={
                        "kind": "tool_use_input",
                        "tool_use_id": event["id"],
                        "json_delta": event["json_delta"],
                    }
                )
        elif typ == "tool_use_end":
            meta = tool_meta.pop(event["id"], None)
            raw = tool_inputs.pop(event["id"], "")
            if meta is not None:
                try:
                    parsed = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    parsed = {"__invalid_json": True, "raw": raw}
                content.append(ToolUseBlock(id=meta[0], name=meta[1], input=parsed))
        elif typ == "message_end":
            flush_text()
            flush_thinking()
            stop_reason = event["stop_reason"]
            usage = event["usage"]
            metadata = event.get("provider_metadata")

    message = Message(role="assistant", content=content, provider_metadata=metadata)
    yield AssistantAssembly(message=message, stop_reason=stop_reason, usage=usage)


async def run_loop(session: Session, prompt: str, opts: RunOptions) -> AsyncIterator[Event]:
    agent = session.agent
    run_id = str(uuid4())
    session.active_run_id = run_id
    started = time.time()
    total = Usage()

    # Resolve per-run deps: RunOptions.deps wins over Agent.deps
    session.run_deps = opts.deps if opts.deps is not None else getattr(agent, "deps", None)

    # Resolve final_tool_name: RunOptions wins over Agent
    effective_final_tool = opts.final_tool_name or getattr(agent, "final_tool_name", None)

    yield SystemEvent(
        session_id=session.id,
        run_id=run_id,
        model=agent.model,
        tools=sorted(tool.name for tool in agent.tools.list()),
        permission_mode=agent.permission_engine.mode,
        cwd=agent.cwd,
    )

    if not session.skills_loaded_emitted and agent.skills:
        session.skills_loaded_emitted = True
        skills_data = [
            {
                "name": s.name,
                "description": s.frontmatter.description,
                **({"when_to_use": s.frontmatter.when_to_use} if s.frontmatter.when_to_use else {}),
                **(
                    {"argument_hint": s.frontmatter.argument_hint}
                    if s.frontmatter.argument_hint
                    else {}
                ),
            }
            for s in sorted(agent.skills.values(), key=lambda x: x.name)
        ]
        yield SkillsLoadedEvent(skills=skills_data)

    user_message = build_user_message(prompt, opts.images)
    if agent.skill_listing_text and not session.tools_override:
        from .skills.system_reminder import wrap_in_system_reminder

        reminder = wrap_in_system_reminder(agent.skill_listing_text)
        user_message.content.insert(0, TextBlock(text=reminder))
    await session.append([user_message])
    yield UserEvent(message=user_message)

    from .abort import throw_if_aborted

    max_turns = int(agent.max_turns) if isinstance(agent.max_turns, int) else 10**9
    signal = session._abort_controller
    try:
        for turn_index in range(max_turns):
            throw_if_aborted(signal)
            session.compaction_retry_used_this_turn = False

            pending = session.pending_skill_overlay
            session.pending_skill_overlay = None
            model_override = None
            if pending is not None:
                session.current_turn_allowed_tools = pending.allowed_tools
                model_override = pending.model_override
            else:
                session.current_turn_allowed_tools = None

            if await maybe_compact(session, agent, signal):
                yield build_compaction_event(session)
                _re_inject_skill_context(session)

            # ── Context injection (RAG-per-turn, schema injection, …) ─────
            extra_system: list = []
            await _run_context_injectors(session, turn_index, extra_system)

            req = _build_turn_request(
                session, opts, extra_system=extra_system, model_override=model_override
            )

            assembly: AssistantAssembly | None = None
            try:
                async for item in stream_turn(session, req):
                    if isinstance(item, AssistantAssembly):
                        assembly = item
                    else:
                        yield item
            except ContextLengthError:
                if not session.compaction_retry_used_this_turn:
                    session.mark_compaction_used()
                    await run_forced_compaction(session, agent, signal)
                    yield build_compaction_event(session)
                    _re_inject_skill_context(session)
                    # Re-run injectors after compaction so fresh context lands
                    extra_system = []
                    await _run_context_injectors(session, turn_index, extra_system)
                    req = _build_turn_request(session, opts, extra_system=extra_system)
                    assembly = None
                    async for item in stream_turn(session, req):
                        if isinstance(item, AssistantAssembly):
                            assembly = item
                        else:
                            yield item
                else:
                    raise

            if assembly is None:
                raise RuntimeError("provider stream ended without assistant assembly")

            await session.append([assembly.message])
            yield AssistantEvent(message=assembly.message, stop_reason=assembly.stop_reason)
            total = total.add(assembly.usage)
            session.last_usage = assembly.usage
            yield UsageEvent(usage=assembly.usage, cumulative=total)

            # ── Check for final_tool (terminal tool-use) ──────────────────
            if effective_final_tool and assembly.stop_reason == "tool_use":
                tool_blocks = [b for b in assembly.message.content if isinstance(b, ToolUseBlock)]
                final_block = next((b for b in tool_blocks if b.name == effective_final_tool), None)
                if final_block is not None:
                    # Terminal: treat the tool input as structured output,
                    # do NOT execute it as a normal tool call.
                    yield ResultEvent(
                        subtype="success",
                        stop_reason="tool_use",
                        total_usage=total,
                        duration_ms=int((time.time() - started) * 1000),
                        final_text=None,
                        structured_output=final_block.input,
                    )
                    return

            # ── Normal text response (stop_reason != tool_use) ───────────
            if assembly.stop_reason != "tool_use":
                ft = final_text(assembly.message)
                structured_output = None
                structured_error = None
                effective_schema = opts.output_schema or getattr(agent, "output_schema", None)
                if effective_schema is not None and ft is not None:
                    structured_output, structured_error = _parse_structured_output(
                        ft, effective_schema
                    )

                yield ResultEvent(
                    subtype="success",
                    stop_reason=assembly.stop_reason,
                    total_usage=total,
                    duration_ms=int((time.time() - started) * 1000),
                    final_text=ft,
                    structured_output=structured_output,
                    structured_error=structured_error,
                )
                return

            tool_blocks = [b for b in assembly.message.content if isinstance(b, ToolUseBlock)]
            result_blocks: list[ToolResultBlock] = []
            async for event in execute_tool_calls(
                tool_blocks,
                agent,
                session,
                signal,
            ):
                yield event
                if isinstance(event, ToolCallEndEvent):
                    result_blocks.append(
                        ToolResultBlock(
                            tool_use_id=event.tool_use_id,
                            content=event.result,
                            is_error=event.is_error,
                        )
                    )
            result_message = Message(role="user", content=result_blocks)
            await session.append([result_message])
            yield UserEvent(message=result_message)

        yield ErrorEvent(
            error={
                "name": "TurnLimitError",
                "message": "max turns exceeded",
                "retryable": False,
            }
        )
        yield ResultEvent(
            subtype="error",
            stop_reason="error",
            total_usage=total,
            duration_ms=int((time.time() - started) * 1000),
        )
    except AbortError:
        yield ResultEvent(
            subtype="aborted",
            stop_reason="error",
            total_usage=total,
            duration_ms=int((time.time() - started) * 1000),
        )
    except Exception as exc:
        retryable = getattr(exc, "retryable", False)
        status = getattr(exc, "status", None)
        yield ErrorEvent(
            error={
                "name": exc.__class__.__name__,
                "message": str(exc),
                "retryable": retryable,
                **({"status": status} if isinstance(status, int) else {}),
            }
        )
        yield ResultEvent(
            subtype="error",
            stop_reason="error",
            total_usage=total,
            duration_ms=int((time.time() - started) * 1000),
        )
