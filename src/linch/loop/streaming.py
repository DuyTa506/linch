"""Provider streaming: assemble one assistant turn from stream events, with
ContextLengthError recovery (legacy single retry or compaction ladder)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any, cast

from ..compaction import (
    apply_micro_compaction,
    build_compaction_event,
    maybe_compact,
    reset_read_tracker_after_compaction,
    run_forced_compaction,
)
from ..context import context_budget_to_dict
from ..errors import ContextLengthError, ProviderError
from ..events import ContextBuildEvent, ModelFallbackEvent, PartialAssistantEvent
from ..providers.limiter import provider_slot
from ..providers.retry import RetryOptions, _delay_for_error
from ..session import RunOptions, Session
from ..types import (
    AssistantAssembly,
    ContentBlock,
    Message,
    ProviderRequest,
    RedactedThinkingBlock,
    StopReason,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    Usage,
)
from .request import (
    _build_context_result,
    _build_turn_request,
    _context_selected_tool_names,
    _re_inject_skill_context,
    apply_provider_capabilities,
)

# Transport/stream-parsing hiccups (a malformed mid-stream tool-call delta, a
# dropped connection) are typically resolved by an immediate re-request, unlike
# rate-limit backoff which needs multi-second waits — so this path uses a much
# shorter base delay than providers.retry's RetryOptions() default (1s-30s,
# tuned for RateLimitError). Still exponential-with-jitter so a run of failures
# doesn't hammer the server.
_SAME_MODEL_RETRY_OPTIONS = RetryOptions(base_delay_ms=100, max_delay_ms=2000, jitter=0.2)


async def _retry_same_model(exc: Exception, attempts: list[int], agent: Any) -> bool:
    """Back off and report "retry" for a *retryable* ``ProviderError`` when no
    fallback model is configured (or fallbacks are exhausted).

    ``agent.provider.stream()`` can throw a retryable, transport-level
    ``ProviderError`` mid-stream — e.g. an OpenAI-compatible server (llama.cpp,
    vLLM, SGLang) emitting a malformed tool-call delta under concurrent load.
    Before this helper, ``_apply_model_fallback`` returning ``None`` (no
    fallback_models configured) meant the error was always re-raised, even
    though ``agent.max_retries`` promises retry budget and the error is
    explicitly marked ``retryable``. Bounded by ``agent.max_retries`` (same
    knob already threaded to ``ProviderRequest.max_retries``)."""
    if attempts[0] >= agent.max_retries:
        return False
    attempts[0] += 1
    delay_ms = _delay_for_error(exc, attempts[0] - 1, _SAME_MODEL_RETRY_OPTIONS)
    if delay_ms > 0:
        await asyncio.sleep(delay_ms / 1000.0)
    return True


async def maybe_compact_resilient(
    session: Session,
    agent: Any,
    signal: Any,
    *,
    model: str | None = None,
) -> bool:
    """Run proactive (pre-limit) compaction with the same transient-failure
    backoff the turn loop gets, then degrade gracefully instead of crashing.

    ``maybe_compact`` makes its own provider call (summarization) outside the
    turn's retry ladder, so a transient ``ProviderError`` (rate limit, 5xx)
    used to propagate straight out of the run loop even though the identical
    failure is retried when raised from the main turn call. Since this call
    fires *before* the hard context limit is hit, a persistent failure here is
    not fatal: skip compaction for this turn and let the run proceed — the
    reactive ``ContextLengthError`` ladder in ``_stream_turn_with_ladder``
    still has its own compaction+retry path if the turn call runs out of room.
    """
    attempts = [0]
    while True:
        try:
            return await maybe_compact(session, agent, signal, model=model)
        except ProviderError as exc:
            if not getattr(exc, "retryable", False):
                return False
            if await _retry_same_model(exc, attempts, agent):
                continue
            return False


def _apply_model_fallback(
    session: Any, agent: Any, req: Any, exc: Exception
) -> ModelFallbackEvent | None:
    """Swap *req* to the next fallback model after an overload; ``None`` if none left.

    Run-level: records the swap on ``session.active_model`` so every later turn
    uses the new model, and re-applies provider capability downgrades for it. The
    request body (messages/system/tools) is untouched — only the model changes.
    A no-op (returns ``None``) when ``agent.fallback_models`` is empty/exhausted,
    which keeps the default path byte-identical.
    """
    fallbacks = getattr(agent, "fallback_models", None)
    if not fallbacks:
        return None
    index = getattr(session, "fallback_index", 0)
    if index >= len(fallbacks):
        return None
    next_model = fallbacks[index]
    from_model = req.model
    session.fallback_index = index + 1
    session.active_model = next_model
    req.model = next_model
    if hasattr(agent.provider, "capabilities"):
        apply_provider_capabilities(req, agent.provider.capabilities(next_model))
    return ModelFallbackEvent(from_model=from_model, to_model=next_model, reason=str(exc))


async def _stream_turn_with_ladder(
    session: Session,
    agent: Any,
    opts: RunOptions,
    req: Any,
    *,
    turn_index: int,
    signal: Any,
    ladder: Any,
    forced_used: list[int],
    save_checkpoint: Any,
    start_provider_call: Any,
    end_provider_call: Any,
) -> AsyncIterator[Any]:
    """Provider-call attempt loop with compaction-ladder recovery.

    Yields the same items as :func:`stream_turn` (events + the final
    ``AssistantAssembly``) plus recovery ``CompactionEvent``/
    ``ContextBuildEvent`` items.  Nothing is persisted here — the caller
    persists every non-assembly item it receives.

    Rung 1: micro-compact (LLM-free, once per turn).  Rung 2: forced
    compaction, capped per run by ``ladder.max_forced_compactions`` via the
    *forced_used* one-element counter cell; once exhausted the
    ``ContextLengthError`` surfaces.
    """
    micro_tried_this_turn = False
    same_model_retries = [0]
    while True:
        await save_checkpoint("provider_pending", turn_index=turn_index)
        await start_provider_call(turn_index, req.model)
        try:
            async for item in stream_turn(session, req):
                yield item
            return
        except ProviderError as exc:
            # Provider overload (retryable): swap to the next fallback model for
            # the rest of the run and retry; surface the error if none remain.
            if not getattr(exc, "retryable", False):
                raise
            await end_provider_call(stop_reason="error")
            fallback_event = _apply_model_fallback(session, agent, req, exc)
            if fallback_event is not None:
                yield fallback_event
                continue
            # No fallback model configured/left: retry the SAME model/request
            # (bounded by agent.max_retries) instead of surfacing immediately —
            # covers transport-level hiccups (e.g. a malformed mid-stream
            # tool-call diff from an OpenAI-compatible server).
            if await _retry_same_model(exc, same_model_retries, agent):
                continue
            raise
        except ContextLengthError:
            await end_provider_call(stop_reason="context_length_error")
            recovered = False
            if ladder.micro and not micro_tried_this_turn:
                micro_tried_this_turn = True
                recovered = apply_micro_compaction(
                    session,
                    agent,
                    keep_recent_turns=ladder.keep_recent_turns,
                    model=req.model,
                )
            if not recovered:
                if forced_used[0] >= ladder.max_forced_compactions:
                    raise
                forced_used[0] += 1
                await run_forced_compaction(session, agent, signal, model=req.model)
            reset_read_tracker_after_compaction(session, agent)
            _re_inject_skill_context(session)
            # Yield only after the provider view reaches its final post-
            # compaction shape.  The runner persists a snapshot while this
            # generator is suspended at the event.
            yield build_compaction_event(session)
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


async def _stream_turn_with_compaction_retry(
    session: Session,
    agent: Any,
    opts: RunOptions,
    req: Any,
    *,
    turn_index: int,
    signal: Any,
    save_checkpoint: Any,
    start_provider_call: Any,
    end_provider_call: Any,
) -> AsyncIterator[Any]:
    """Legacy provider-call path: a single forced-compaction retry per turn.

    Yields the same items as :func:`stream_turn` plus recovery events; the
    caller persists every non-assembly item.  Behavior is identical to the
    pre-ladder inline code (pinned by tests/loop/test_compaction_ladder.py).
    """
    # The outer loop re-iterates on a successful model-fallback swap (overload
    # recovery) or a same-model retry (see _retry_same_model); with neither
    # configured/available it runs exactly once, keeping the legacy
    # single-compaction-retry behavior byte-identical.
    same_model_retries = [0]
    while True:
        await save_checkpoint("provider_pending", turn_index=turn_index)
        await start_provider_call(turn_index, req.model)
        try:
            try:
                async for item in stream_turn(session, req):
                    yield item
            except ContextLengthError:
                if session.compaction_retry_used_this_turn:
                    raise
                await end_provider_call(stop_reason="context_length_error")
                session.mark_compaction_used()
                await run_forced_compaction(session, agent, signal, model=req.model)
                _re_inject_skill_context(session)
                yield build_compaction_event(session)
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
                await save_checkpoint("provider_pending", turn_index=turn_index)
                await start_provider_call(turn_index, req.model)
                async for item in stream_turn(session, req):
                    yield item
        except ProviderError as exc:
            if not getattr(exc, "retryable", False):
                raise
            await end_provider_call(stop_reason="error")
            fallback_event = _apply_model_fallback(session, agent, req, exc)
            if fallback_event is not None:
                yield fallback_event
                continue
            if await _retry_same_model(exc, same_model_retries, agent):
                continue
            raise
        return


async def stream_turn(
    session: Session, req: ProviderRequest
) -> AsyncIterator[PartialAssistantEvent | AssistantAssembly]:
    """Stream one provider turn, holding the agent's provider slot throughout.

    The gate lives here rather than around the caller so that a retry releases
    it while ``_retry_same_model`` backs off, and so an abandoned run hands its
    slot back: closing this generator unwinds the ``async with`` deterministically.
    """
    async with provider_slot(session.agent, req.model):
        async for item in _stream_turn(session, req):
            yield item


async def _stream_turn(
    session: Session, req: ProviderRequest
) -> AsyncIterator[PartialAssistantEvent | AssistantAssembly]:
    agent = session.agent
    stream_partials = (
        agent.include_partial_messages if req.stream_partials is None else req.stream_partials
    )
    text_buf: list[str] = []
    thinking_buf: list[str] = []
    thinking_sig: str | None = None
    tool_inputs: dict[str, list[str]] = {}
    tool_meta: dict[str, tuple[str, str]] = {}
    content: list[ContentBlock] = []
    stop_reason: StopReason = "end_turn"
    usage = Usage()
    metadata: dict[str, Any] | None = None
    message_started = False
    message_ended = False
    seen_tool_ids: set[str] = set()

    def protocol_error(detail: str) -> ProviderError:
        return ProviderError(f"invalid normalized provider stream: {detail}", retryable=False)

    def flush_text() -> None:
        nonlocal text_buf
        if text_buf:
            content.append(TextBlock(text="".join(text_buf)))
            text_buf = []

    def flush_thinking() -> None:
        nonlocal thinking_buf, thinking_sig
        if thinking_buf:
            content.append(ThinkingBlock(thinking="".join(thinking_buf), signature=thinking_sig))
            thinking_buf = []
            thinking_sig = None

    async for event in agent.provider.stream(req):
        if not isinstance(event, dict):
            raise protocol_error(f"event must be a dict, got {type(event).__name__}")
        typ = event.get("type")
        if not isinstance(typ, str):
            raise protocol_error("event.type must be a string")
        if message_ended:
            raise protocol_error(f"event {typ!r} appeared after message_end")
        if typ == "message_start":
            if message_started:
                raise protocol_error("duplicate message_start")
            model = event.get("model")
            if not isinstance(model, str) or not model:
                raise protocol_error("message_start.model must be a non-empty string")
            message_started = True
            continue
        if not message_started:
            raise protocol_error(f"first event must be message_start, got {typ!r}")
        if typ == "text_delta":
            flush_thinking()
            if not isinstance(event.get("text"), str):
                raise protocol_error("text_delta.text must be a string")
            text = cast(str, event["text"])
            text_buf.append(text)
            if stream_partials:
                yield PartialAssistantEvent(delta={"kind": "text", "text": text})
        elif typ == "thinking_delta":
            flush_text()
            if not isinstance(event.get("text"), str):
                raise protocol_error("thinking_delta.text must be a string")
            text = cast(str, event["text"])
            thinking_buf.append(text)
            signature = event.get("signature", thinking_sig)
            thinking_sig = signature if isinstance(signature, str) else thinking_sig
            if stream_partials:
                yield PartialAssistantEvent(delta={"kind": "thinking", "text": text})
        elif typ == "redacted_thinking":
            flush_text()
            flush_thinking()
            content.append(RedactedThinkingBlock(data=str(event.get("data", ""))))
        elif typ == "tool_use_start":
            flush_text()
            flush_thinking()
            tool_id = event.get("id")
            name = event.get("name")
            if not isinstance(tool_id, str) or not tool_id:
                raise protocol_error("tool_use_start.id must be a non-empty string")
            if not isinstance(name, str) or not name:
                raise protocol_error("tool_use_start.name must be a non-empty string")
            if tool_id in seen_tool_ids:
                raise protocol_error(f"duplicate tool-use id {tool_id!r}")
            seen_tool_ids.add(tool_id)
            tool_meta[tool_id] = (tool_id, name)
            tool_inputs[tool_id] = []
        elif typ == "tool_use_input_delta":
            tool_id = event.get("id")
            json_delta = event.get("json_delta")
            if not isinstance(tool_id, str) or tool_id not in tool_meta:
                raise protocol_error(
                    f"tool_use_input_delta references unknown or closed id {tool_id!r}"
                )
            if not isinstance(json_delta, str):
                raise protocol_error("tool_use_input_delta.json_delta must be a string")
            tool_inputs[tool_id].append(json_delta)
            if stream_partials:
                yield PartialAssistantEvent(
                    delta={
                        "kind": "tool_use_input",
                        "tool_use_id": tool_id,
                        "json_delta": json_delta,
                    }
                )
        elif typ == "tool_use_end":
            tool_id = event.get("id")
            if not isinstance(tool_id, str) or tool_id not in tool_meta:
                raise protocol_error(f"tool_use_end references unknown or closed id {tool_id!r}")
            meta = tool_meta.pop(tool_id)
            raw = "".join(tool_inputs.pop(tool_id))
            try:
                parsed = json.loads(raw) if raw else {}
            except json.JSONDecodeError as exc:
                raise protocol_error(f"tool {tool_id!r} input is invalid JSON: {exc}") from exc
            if not isinstance(parsed, dict):
                raise protocol_error(f"tool {tool_id!r} input must decode to a JSON object")
            content.append(ToolUseBlock(id=meta[0], name=meta[1], input=parsed))
        elif typ == "message_end":
            if tool_meta:
                raise protocol_error(
                    "message_end arrived before tool_use_end for " + ", ".join(sorted(tool_meta))
                )
            raw_stop = event.get("stop_reason")
            if raw_stop not in {
                "end_turn",
                "tool_use",
                "max_tokens",
                "stop_sequence",
                "refusal",
                "error",
                "interrupted",
            }:
                raise protocol_error(f"invalid message_end.stop_reason {raw_stop!r}")
            raw_usage = event.get("usage")
            if not isinstance(raw_usage, Usage):
                raise protocol_error("message_end.usage must be a Usage instance")
            flush_text()
            flush_thinking()
            stop_reason = cast(StopReason, raw_stop)
            usage = raw_usage
            raw_metadata = event.get("provider_metadata")
            if raw_metadata is not None and not isinstance(raw_metadata, dict):
                raise protocol_error("message_end.provider_metadata must be a dict or None")
            metadata = raw_metadata
            message_ended = True
        else:
            raise protocol_error(f"unknown event type {typ!r}")

    if not message_started:
        raise protocol_error("stream ended before message_start")
    if not message_ended:
        raise protocol_error("stream ended before message_end")
    complete_tool_calls = [block for block in content if isinstance(block, ToolUseBlock)]
    if stop_reason == "tool_use" and not complete_tool_calls:
        raise protocol_error("stop_reason='tool_use' requires at least one complete tool call")
    if stop_reason != "tool_use" and complete_tool_calls:
        raise protocol_error(
            f"stream completed tool calls but stop_reason was {stop_reason!r}, not 'tool_use'"
        )

    message = Message(role="assistant", content=content, provider_metadata=metadata)
    yield AssistantAssembly(message=message, stop_reason=stop_reason, usage=usage)
