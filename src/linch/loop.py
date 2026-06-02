from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

from .compaction import build_compaction_event, maybe_compact, run_forced_compaction
from .context import (
    ContextBuildResult,
    ContextBuildTurn,
    apply_context_budget,
    context_budget_to_dict,
    normalize_context_builder,
)
from .errors import AbortError, ContextLengthError
from .events import (
    AssistantEvent,
    ContextBuildEvent,
    ErrorEvent,
    Event,
    LoopGuardEvent,
    PartialAssistantEvent,
    ResultEvent,
    SkillsLoadedEvent,
    SystemEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
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


async def _build_context_result(session: Session, turn_index: int) -> ContextBuildResult | None:
    agent = session.agent
    builder = normalize_context_builder(getattr(agent, "context_builder", None))
    if builder is None:
        return None

    turn = ContextBuildTurn(
        session=session,
        messages=list(session.provider_view),
        turn_index=turn_index,
        deps=getattr(session, "run_deps", None),
        model=agent.model,
        tools=getattr(session, "tools_override", None) or agent.tools,
        token_estimator=getattr(agent, "token_estimator", None),
    )
    result = await builder.build(turn)
    return apply_context_budget(
        result,
        estimator=getattr(agent, "token_estimator", None),
        model=agent.model,
    )


def apply_provider_capabilities(req: ProviderRequest, caps: Any) -> ProviderRequest:
    """Downgrade *req* fields to match what *caps* says the provider supports.

    * ``prompt_cache=False`` → clears ``req.cache_prompt`` and
      ``req.cache_ttl`` so providers that ignore caching don't receive dead
      flags (fixes current dead-plumbing where every request sends
      ``cache_prompt=True`` regardless of provider).
    * ``tool_choice=False`` → clears ``req.tool_choice``.
    * ``structured_output=False`` → clears ``req.output_schema``; the loop
      still text-parses using ``opts/agent.output_schema`` at
      :func:`run_loop` line ~452, so the host's intent is preserved.
    * ``parallel_tool_calls`` is informational and has no ``req`` field yet.

    Modifies *req* in place and returns it.
    """
    if not caps.prompt_cache:
        req.cache_prompt = None
        req.cache_ttl = None
    if not caps.tool_choice:
        req.tool_choice = None
    if not caps.structured_output:
        req.output_schema = None
    return req


def _build_turn_request(
    session: Session,
    opts: RunOptions,
    *,
    context: ContextBuildResult | None = None,
    model_override: str | None = None,
) -> ProviderRequest:
    """Build the :class:`ProviderRequest` for one provider call.

    Collapses the two near-identical request builders (normal path and
    ContextLengthError retry path) into one place.  Applies provider
    capability downgrades before returning.
    """
    agent = session.agent

    base_system = list(session.system_blocks_override or agent.system_blocks)
    if context and context.system_blocks:
        base_system = base_system + list(context.system_blocks)

    messages = list(session.provider_view)
    if context and context.messages:
        messages.extend(context.messages)

    tools = _select_context_tools(session, context)

    req = ProviderRequest(
        model=model_override or agent.model,
        system=base_system,
        tools=tools.schemas(),
        messages=messages,
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

    # Apply provider capability downgrades (e.g. clear cache_prompt for
    # providers that don't support it, strip output_schema when the
    # provider has no native structured output, etc.).
    if hasattr(agent.provider, "capabilities"):
        caps = agent.provider.capabilities(req.model)
        apply_provider_capabilities(req, caps)

    return req


def _select_context_tools(session: Session, context: ContextBuildResult | None) -> Any:
    registry = session.tools_override or session.agent.tools
    if context is None or context.selected_tools is None:
        return registry

    selected = context.selected_tools
    if hasattr(selected, "schemas") and hasattr(selected, "get"):
        return selected
    if isinstance(selected, str):
        return registry.select(names={selected})
    if isinstance(selected, dict):
        names = selected.get("names")
        tags = selected.get("tags")
        return registry.select(
            names={str(name) for name in names} if isinstance(names, (list, set, tuple)) else None,
            tags={str(tag) for tag in tags} if isinstance(tags, (list, set, tuple)) else None,
        )
    if isinstance(selected, (list, set, tuple)):
        return registry.select(names={str(name) for name in selected})
    return registry


def _context_selected_tool_names(context: ContextBuildResult | None) -> list[str] | None:
    if context is None or context.selected_tools is None:
        return None
    selected = context.selected_tools
    if hasattr(selected, "list"):
        return sorted(tool.name for tool in selected.list())
    if isinstance(selected, str):
        return [selected]
    if isinstance(selected, dict):
        names = selected.get("names")
        tags = selected.get("tags")
        parts: list[str] = []
        if isinstance(names, (list, set, tuple)):
            parts.extend(str(name) for name in names)
        if isinstance(tags, (list, set, tuple)):
            parts.extend(f"tag:{tag}" for tag in tags)
        return sorted(parts)
    if isinstance(selected, (list, set, tuple)):
        return sorted(str(name) for name in selected)
    return None


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
                    if not isinstance(parsed, dict):
                        parsed = {}
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

    # ── Observability hub ─────────────────────────────────────────────────
    from .observability import ObserverDispatcher
    from .observability import ProviderCallInfo as _ProviderCallInfo
    from .observability import ProviderCallResult as _ProviderCallResult
    from .observability import RunInfo as _RunInfo
    from .observability import RunResultInfo as _RunResultInfo
    from .observability import ToolInfo as _ToolInfo
    from .observability import ToolResultInfo as _ToolResultInfo
    from .observability import TurnInfo as _TurnInfo

    hub = ObserverDispatcher(getattr(agent, "observers", None))

    # Resolve per-run deps: RunOptions.deps wins over Agent.deps
    session.run_deps = opts.deps if opts.deps is not None else getattr(agent, "deps", None)

    # Resolve final_tool_name: RunOptions wins over Agent
    effective_final_tool = opts.final_tool_name or getattr(agent, "final_tool_name", None)

    # Loop guard — detects repeated identical tool calls and consecutive
    # failure streaks.  On by default (Agent sets self.loop_guard = LoopGuard()
    # unless the caller passes loop_guard=None).
    from .loop_guard import LoopGuardState, evaluate_loop_guard

    _guard = getattr(agent, "loop_guard", None)
    _guard_state = LoopGuardState() if _guard is not None else None
    _force_final_pending = False

    if hub.active:
        await hub.dispatch(
            "on_run_start",
            _RunInfo(
                run_id=run_id,
                session_id=session.id,
                model=agent.model,
                prompt=prompt,
                tools=tuple(sorted(tool.name for tool in agent.tools.list())),
            ),
        )

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
    _final_result: _RunResultInfo | None = None
    _active_turn_index: int | None = None
    _active_provider_call: tuple[int, str, float] | None = None

    async def _start_turn(turn_index: int) -> None:
        nonlocal _active_turn_index
        if hub.active:
            await hub.dispatch("on_turn_start", _TurnInfo(run_id=run_id, turn_index=turn_index))
            _active_turn_index = turn_index

    async def _end_active_turn() -> None:
        nonlocal _active_turn_index
        if not hub.active or _active_turn_index is None:
            return
        turn_index = _active_turn_index
        _active_turn_index = None
        await hub.dispatch("on_turn_end", _TurnInfo(run_id=run_id, turn_index=turn_index))

    async def _start_provider_call(turn_index: int, model: str) -> None:
        nonlocal _active_provider_call
        started_at = time.perf_counter()
        if hub.active:
            await hub.dispatch(
                "on_provider_call_start",
                _ProviderCallInfo(run_id=run_id, turn_index=turn_index, model=model),
            )
            _active_provider_call = (turn_index, model, started_at)

    async def _end_active_provider_call(*, stop_reason: str, usage: Usage | None = None) -> None:
        nonlocal _active_provider_call
        if not hub.active or _active_provider_call is None:
            return
        turn_index, model, started_at = _active_provider_call
        _active_provider_call = None
        await hub.dispatch(
            "on_provider_call_end",
            _ProviderCallResult(
                run_id=run_id,
                turn_index=turn_index,
                model=model,
                stop_reason=stop_reason,
                usage=usage or Usage(),
                duration_ms=int((time.perf_counter() - started_at) * 1000),
            ),
        )

    async def _close_active_observer_spans(*, stop_reason: str = "error") -> None:
        await _end_active_provider_call(stop_reason=stop_reason)
        await _end_active_turn()

    try:
        for turn_index in range(max_turns):
            throw_if_aborted(signal)
            await _start_turn(turn_index)
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

            # ── Context building (RAG-per-turn, schema injection, …) ──────
            context_result = await _build_context_result(session, turn_index)
            if context_result is not None:
                yield ContextBuildEvent(
                    system_blocks=len(context_result.system_blocks),
                    messages=len(context_result.messages),
                    selected_tools=_context_selected_tool_names(context_result),
                    budget=context_budget_to_dict(context_result.budget),
                    metadata=dict(context_result.metadata),
                )

            req = _build_turn_request(
                session, opts, context=context_result, model_override=model_override
            )

            # If the previous turn tripped the guard with force_final, strip
            # all tools so the model must produce a text response.
            if _force_final_pending:
                req.tools = []
                req.tool_choice = None
                _force_final_pending = False

            assembly: AssistantAssembly | None = None
            await _start_provider_call(turn_index, req.model)
            try:
                async for item in stream_turn(session, req):
                    if isinstance(item, AssistantAssembly):
                        assembly = item
                    else:
                        yield item
            except ContextLengthError:
                if not session.compaction_retry_used_this_turn:
                    await _end_active_provider_call(stop_reason="context_length_error")
                    session.mark_compaction_used()
                    await run_forced_compaction(session, agent, signal)
                    yield build_compaction_event(session)
                    _re_inject_skill_context(session)
                    # Re-run context builders after compaction so fresh context lands.
                    context_result = await _build_context_result(session, turn_index)
                    if context_result is not None:
                        yield ContextBuildEvent(
                            system_blocks=len(context_result.system_blocks),
                            messages=len(context_result.messages),
                            selected_tools=_context_selected_tool_names(context_result),
                            budget=context_budget_to_dict(context_result.budget),
                            metadata=dict(context_result.metadata),
                        )
                    req = _build_turn_request(session, opts, context=context_result)
                    assembly = None
                    await _start_provider_call(turn_index, req.model)
                    async for item in stream_turn(session, req):
                        if isinstance(item, AssistantAssembly):
                            assembly = item
                        else:
                            yield item
                else:
                    raise

            if assembly is None:
                raise RuntimeError("provider stream ended without assistant assembly")

            await _end_active_provider_call(
                stop_reason=assembly.stop_reason,
                usage=assembly.usage,
            )

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
                    _dur = int((time.time() - started) * 1000)
                    _final_result = _RunResultInfo(
                        run_id=run_id,
                        session_id=session.id,
                        subtype="success",
                        stop_reason="tool_use",
                        total_usage=total,
                        duration_ms=_dur,
                    )
                    await _end_active_turn()
                    yield ResultEvent(
                        subtype="success",
                        stop_reason="tool_use",
                        total_usage=total,
                        duration_ms=_dur,
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

                _dur = int((time.time() - started) * 1000)
                _final_result = _RunResultInfo(
                    run_id=run_id,
                    session_id=session.id,
                    subtype="success",
                    stop_reason=assembly.stop_reason,
                    total_usage=total,
                    duration_ms=_dur,
                )
                await _end_active_turn()
                yield ResultEvent(
                    subtype="success",
                    stop_reason=assembly.stop_reason,
                    total_usage=total,
                    duration_ms=_dur,
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
                turn_index=turn_index,
            ):
                yield event
                if hub.active:
                    if isinstance(event, ToolCallStartEvent):
                        await hub.dispatch(
                            "on_tool_start",
                            _ToolInfo(
                                run_id=run_id,
                                turn_index=turn_index,
                                tool_use_id=event.tool_use_id,
                                tool_name=event.tool_name,
                                input=event.input,
                                summary=event.summary,
                            ),
                        )
                    elif isinstance(event, ToolCallEndEvent):
                        await hub.dispatch(
                            "on_tool_end",
                            _ToolResultInfo(
                                run_id=run_id,
                                turn_index=turn_index,
                                tool_use_id=event.tool_use_id,
                                tool_name=event.tool_name,
                                is_error=event.is_error,
                                duration_ms=event.duration_ms,
                                result=event.result,
                                tool_result=event.tool_result,
                            ),
                        )
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

            # ── Loop guard evaluation ─────────────────────────────────────
            if _guard is not None and _guard_state is not None:
                _decision = evaluate_loop_guard(_guard, _guard_state, tool_blocks, result_blocks)
                if _decision.action != "continue":
                    yield LoopGuardEvent(
                        reason=_decision.reason,
                        detail=_decision.detail,
                        action=_decision.action,
                    )
                    if _decision.action == "force_final":
                        # Inject a reminder so the model knows to summarise
                        # without further tool calls, then let the loop run
                        # one more tools-disabled turn.
                        from .skills.system_reminder import wrap_in_system_reminder

                        _reminder_text = wrap_in_system_reminder(
                            "You appear to be stuck in a loop or repeatedly "
                            "encountering failures. Please provide your final "
                            "answer now without making further tool calls."
                        )
                        _reminder_msg = Message(
                            role="user", content=[TextBlock(text=_reminder_text)]
                        )
                        await session.append([_reminder_msg])
                        yield UserEvent(message=_reminder_msg)
                        _force_final_pending = True
                    else:
                        # Hard stop — emit error result and exit.
                        _dur = int((time.time() - started) * 1000)
                        _final_result = _RunResultInfo(
                            run_id=run_id,
                            session_id=session.id,
                            subtype="error",
                            stop_reason="error",
                            total_usage=total,
                            duration_ms=_dur,
                        )
                        await _end_active_turn()
                        yield ResultEvent(
                            subtype="error",
                            stop_reason="error",
                            total_usage=total,
                            duration_ms=_dur,
                        )
                        return
            # Natural end of turn body (guard said "continue" or "force_final").
            await _end_active_turn()

        # ── Max-turns exhausted ───────────────────────────────────────────
        _dur = int((time.time() - started) * 1000)
        _final_result = _RunResultInfo(
            run_id=run_id,
            session_id=session.id,
            subtype="error",
            stop_reason="error",
            total_usage=total,
            duration_ms=_dur,
        )
        yield LoopGuardEvent(
            reason="max_turns",
            detail=f"Maximum turns ({max_turns}) reached.",
            action="stop",
        )
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
            duration_ms=_dur,
        )
    except AbortError:
        _dur = int((time.time() - started) * 1000)
        _final_result = _RunResultInfo(
            run_id=run_id,
            session_id=session.id,
            subtype="aborted",
            stop_reason="error",
            total_usage=total,
            duration_ms=_dur,
        )
        await _close_active_observer_spans(stop_reason="error")
        yield ResultEvent(
            subtype="aborted",
            stop_reason="error",
            total_usage=total,
            duration_ms=_dur,
        )
    except Exception as exc:
        retryable = getattr(exc, "retryable", False)
        status = getattr(exc, "status", None)
        _err_dict: dict[str, object] = {
            "name": exc.__class__.__name__,
            "message": str(exc),
            "retryable": retryable,
            **({"status": status} if isinstance(status, int) else {}),
        }
        _dur = int((time.time() - started) * 1000)
        _final_result = _RunResultInfo(
            run_id=run_id,
            session_id=session.id,
            subtype="error",
            stop_reason="error",
            total_usage=total,
            duration_ms=_dur,
            error=_err_dict,
        )
        await _close_active_observer_spans(stop_reason="error")
        yield ErrorEvent(error=_err_dict)
        yield ResultEvent(
            subtype="error",
            stop_reason="error",
            total_usage=total,
            duration_ms=_dur,
        )
    finally:
        if hub.active:
            await _close_active_observer_spans(stop_reason="error")
            _run_result = _final_result or _RunResultInfo(
                run_id=run_id,
                session_id=session.id,
                subtype="error",
                stop_reason="error",
                total_usage=total,
                duration_ms=int((time.time() - started) * 1000),
            )
            await hub.dispatch("on_run_end", _run_result)
