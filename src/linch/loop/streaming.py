"""Provider streaming: assemble one assistant turn from stream events, with
ContextLengthError recovery (single retry or compaction ladder)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass
from inspect import isawaitable
from time import monotonic
from typing import Any, cast, get_args
from uuid import uuid4

from .._telemetry import ChatCall, in_span
from ..catalog import native_definitions, positional_catalog, snapshot_for, sync_catalog
from ..compaction import (
    apply_micro_compaction,
    build_compaction_event,
    maybe_compact,
    reset_read_tracker_after_compaction,
    run_forced_compaction,
)
from ..context import context_budget_to_dict
from ..errors import AbortError, ContextLengthError, ProviderError
from ..events import ContextBuildEvent, ModelFallbackEvent, PartialAssistantEvent
from ..model_calls import (
    ModelCallIntent,
    ModelCallReceipt,
    _AccountedAuxiliaryCaller,
    _copy_usage,
    _ModelCallError,
    _ModelCallLedger,
    _primary_request_fingerprint,
)
from ..pricing import cost_usd
from ..providers.limiter import provider_slot
from ..providers.retry import RetryOptions, _delay_for_error
from ..run_store import LeaseLostError
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
_STOP_REASONS = frozenset(get_args(StopReason))


@asynccontextmanager
async def _provider_stream_context(stream: Any) -> AsyncIterator[Any]:
    """Close a provider iterator when it exposes an optional async closer."""
    try:
        yield stream
    finally:
        close = getattr(stream, "aclose", None)
        if callable(close):
            result = close()
            if isawaitable(result):
                await result


@dataclass(slots=True)
class _PrimaryStreamState:
    """Private normalized facts, insulated from post-provider hook mutation."""

    actual_model: str = ""
    usage: Usage | None = None
    native_failure: str | None = None
    stop_reason: str | None = None
    message: Message | None = None


@dataclass(slots=True)
class _PrimaryPartial:
    """Live partial event whose durable write awaits full-response validation."""

    event: PartialAssistantEvent
    delivery_attempt_id: str | None = None


@dataclass(slots=True)
class _ParsedPrimaryAssembly:
    assembly: AssistantAssembly
    state: _PrimaryStreamState


@dataclass(slots=True)
class _AccountedPrimaryAssembly:
    """Settled primary result visible only to the run-loop integration."""

    assembly: AssistantAssembly
    receipt: ModelCallReceipt
    rejected_native: bool


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
    auxiliary_caller: _AccountedAuxiliaryCaller | None = None,
    logical_call_id: str | None = None,
    config: Any = None,
) -> bool:
    """Run proactive (pre-limit) compaction with the same transient-failure
    backoff the turn loop gets, then degrade gracefully instead of crashing.

    ``maybe_compact`` makes its own provider call (summarization) outside the
    turn's retry ladder, so a transient ``ProviderError`` (rate limit, 5xx)
    used to propagate straight out of the run loop even though the identical
    failure is retried when raised from the main turn call. Since this call
    fires *before* the hard context limit is hit, a persistent failure here is
    not fatal: skip compaction for this turn and let the run proceed — the
    reactive ``ContextLengthError`` ladder in ``_stream_turn_with_recovery``
    still has its own compaction+retry path if the turn call runs out of room.
    """
    attempts = [0]
    while True:
        try:
            return await maybe_compact(
                session,
                agent,
                signal,
                model=model,
                auxiliary_caller=auxiliary_caller,
                logical_call_id=logical_call_id,
                **(
                    {"_run_config": config}
                    if config is not None and getattr(config, "_values", None) is not None
                    else {}
                ),
            )
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
    uses the new model, and re-applies provider capability downgrades for it.
    Append-only fallback expands eligible frozen native definitions into tools.
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
        caps = agent.provider.capabilities(next_model)
        if req._native_catalog_changes and caps.catalog_changes != "positional":
            names = {tool["name"] for tool in req.tools}
            req.tools = req.tools + [
                tool for name, tool in native_definitions(req.messages).items() if name not in names
            ]
            req._native_catalog_changes = False
        apply_provider_capabilities(req, caps)
    return ModelFallbackEvent(from_model=from_model, to_model=next_model, reason=str(exc))


async def _stream_turn_with_recovery(
    session: Session,
    agent: Any,
    opts: RunOptions,
    req: Any,
    *,
    turn_index: int,
    signal: Any,
    ladder: Any,
    forced_used: list[int],
    prepare_provider_call: Any,
    start_provider_call: Any,
    end_provider_call: Any,
    ledger: _ModelCallLedger,
    logical_call_id: str,
    bind_active_primary: Any,
    request_prepared: bool = False,
    auxiliary_caller: _AccountedAuxiliaryCaller | None = None,
    compaction_logical_call_id: str | None = None,
    config: Any = None,
) -> AsyncGenerator[Any, None]:
    """Recover provider turns with a per-turn retry or a per-run ladder budget.

    Default recovery includes its second prepare/dispatch inside the retryable
    ProviderError boundary. Ladder recovery occurs outside that boundary; only
    its next dispatch re-enters it. Preserve that distinction as well as each
    policy's compaction budget and lifecycle end-call ordering.
    """
    micro_tried_this_turn = False
    same_model_retries = [0]

    async def recover(exc: ContextLengthError) -> AsyncGenerator[Any, None]:
        nonlocal req, micro_tried_this_turn
        if ladder is None and session.compaction_retry_used_this_turn:
            raise exc
        await end_provider_call(stop_reason="context_length_error")
        recovered = False
        if ladder is None:
            session.mark_compaction_used()
        else:
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
                    raise exc
                forced_used[0] += 1
        if not recovered:
            await run_forced_compaction(
                session,
                agent,
                signal,
                model=req.model,
                auxiliary_caller=auxiliary_caller,
                logical_call_id=compaction_logical_call_id,
                **(
                    {"_run_config": config}
                    if config is not None and getattr(config, "_values", None) is not None
                    else {}
                ),
            )
        if ladder is not None:
            reset_read_tracker_after_compaction(session, agent)
        _re_inject_skill_context(session, config=config)
        from ..catalog import reset_catalog_notifications
        from .runtime import maybe_save_provider_snapshot

        if getattr(session, "store", None) is not None:
            await maybe_save_provider_snapshot(session)
        await reset_catalog_notifications(session, config)
        defer_catalog = snapshot_for(session) is not None and positional_catalog(
            session, config, req.model
        )
        if not defer_catalog:
            await sync_catalog(session, config, None, model=req.model)
        # The runner snapshots the final provider view while suspended here.
        yield build_compaction_event(session)
        context_result = await _build_context_result(
            session, turn_index, **({"config": config} if config is not None else {})
        )
        if defer_catalog:
            await sync_catalog(session, config, None, context=context_result, model=req.model)
        if context_result is not None:
            yield ContextBuildEvent(
                system_blocks=len(context_result.system_blocks),
                messages=len(context_result.messages),
                selected_tools=_context_selected_tool_names(context_result),
                budget=context_budget_to_dict(context_result.budget),
                metadata=dict(context_result.metadata),
            )
        req = _build_turn_request(
            session,
            opts,
            context=context_result,
            config=config,
            model_override=req.model,
        )
        if getattr(session, "current_turn_allowed_tools", None) == []:
            req.tools = []
            req.tool_choice = None
            if getattr(req, "_native_catalog_changes", None):
                req._native_catalog_changes = False

    async def dispatch() -> AsyncGenerator[Any, None]:
        async with aclosing(
            _accounted_primary_stream(
                session,
                req,
                ledger=ledger,
                logical_call_id=logical_call_id,
                turn_index=turn_index,
                bind_active_primary=bind_active_primary,
            )
        ) as primary_stream:
            async for item in primary_stream:
                yield item

    prepared = request_prepared
    while True:
        if prepared:
            prepared = False
        else:
            req = await prepare_provider_call(req, turn_index)
        await start_provider_call(turn_index, req.model)
        try:
            try:
                async with aclosing(dispatch()) as dispatch_stream:
                    async for item in dispatch_stream:
                        yield item
            except ContextLengthError as exc:
                if ladder is not None:
                    raise
                async with aclosing(recover(exc)) as recovery_stream:
                    async for item in recovery_stream:
                        yield item
                req = await prepare_provider_call(req, turn_index)
                await start_provider_call(turn_index, req.model)
                async with aclosing(dispatch()) as dispatch_stream:
                    async for item in dispatch_stream:
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
        except ContextLengthError as exc:
            if ladder is None:
                raise
            async with aclosing(recover(exc)) as recovery_stream:
                async for item in recovery_stream:
                    yield item
            continue
        return


def _throw_if_aborted(signal: Any) -> None:
    throw = getattr(signal, "throw_if_aborted", None)
    if callable(throw):
        throw()


def _primary_receipt(
    intent: ModelCallIntent,
    state: _PrimaryStreamState,
    *,
    started_at: float,
    reported: bool,
    cancelled: bool = False,
    rejection_reason: str | None = None,
) -> ModelCallReceipt:
    usage = _copy_usage(state.usage) if reported and state.usage is not None else None
    if reported and usage is None:
        raise _ModelCallError("invalid_state", "reported primary receipt is missing usage")
    if reported:
        reason = state.native_failure or rejection_reason
        disposition = (
            "rejected_native"
            if state.native_failure is not None
            else ("rejected" if reason is not None else "accepted")
        )
        usage_status = "reported"
    else:
        reason = "delivery_cancelled" if cancelled else "delivery_failed"
        disposition = "cancelled" if cancelled else "failed"
        usage_status = "unknown"
    return ModelCallReceipt(
        logical_call_id=intent.logical_call_id,
        delivery_attempt_id=intent.delivery_attempt_id,
        kind="primary",
        run_id=intent.run_id,
        turn_index=intent.turn_index,
        actual_model=state.actual_model,
        usage_status=usage_status,
        usage=usage,
        cost_usd=cost_usd(usage, state.actual_model) if usage is not None else None,
        disposition=disposition,
        rejection_reason=reason,
        accepted_text=None,
        accepted_text_sha256=None,
        duration_ms=max(0, int((monotonic() - started_at) * 1000)),
        request_fingerprint=intent.request_fingerprint,
        strategy_id=None,
        source_context_fingerprint=None,
    )


async def _accounted_primary_stream(
    session: Session,
    req: ProviderRequest,
    *,
    ledger: _ModelCallLedger,
    logical_call_id: str,
    turn_index: int | None,
    bind_active_primary: Any,
) -> AsyncGenerator[_PrimaryPartial | _AccountedPrimaryAssembly, None]:
    """Settle exactly one receipt around the actual primary provider stream."""
    intent = ModelCallIntent(
        logical_call_id=logical_call_id,
        delivery_attempt_id=str(uuid4()),
        parent_logical_call_id=None,
        kind="primary",
        run_id=ledger.run_id,
        turn_index=turn_index,
        requested_model=req.model,
        request_fingerprint=_primary_request_fingerprint(req),
        strategy_id=None,
    )
    if bind_active_primary is not None:
        await bind_active_primary(intent)
    await ledger.begin(intent)
    state = _PrimaryStreamState()
    started_at = monotonic()
    started = terminal_attempted = False
    try:
        async with provider_slot(session.agent, req.model):
            _throw_if_aborted(req.signal)
            ledger.assert_may_start()
            await ledger.mark_dispatch_started(intent)
            # The marker is durable before the physical iterator can advance.
            _throw_if_aborted(req.signal)
            ledger.assert_may_start()
            started = True
            parsed: _ParsedPrimaryAssembly | None = None
            with ChatCall(session, req, state) as chat:
                async with aclosing(
                    in_span(
                        _stream_turn(session, req, state=state, stage_partials=True),
                        chat.span,
                        end_on_exit=False,
                    )
                ) as stream:
                    async for item in stream:
                        if isinstance(item, _ParsedPrimaryAssembly):
                            parsed = item
                            break
                        if isinstance(item, _PrimaryPartial):
                            yield _PrimaryPartial(
                                event=item.event,
                                delivery_attempt_id=intent.delivery_attempt_id,
                            )
                        else:
                            raise RuntimeError(
                                "accounted primary parser yielded an unexpected item"
                            )
            assert parsed is not None
            receipt = _primary_receipt(intent, state, started_at=started_at, reported=True)
            terminal_attempted = True
            await ledger.settle(receipt)
            yield _AccountedPrimaryAssembly(
                assembly=parsed.assembly,
                receipt=receipt,
                rejected_native=state.native_failure is not None,
            )
            return
    except LeaseLostError:
        raise
    except BaseException as exc:
        if not terminal_attempted:
            terminal_attempted = True
            if ledger._persistence_failed:
                pass
            elif not started:
                await ledger.abandon(intent, reason="delivery_not_dispatched", not_dispatched=True)
            else:
                cancelled = isinstance(exc, (AbortError, asyncio.CancelledError, GeneratorExit))
                if state.usage is not None:
                    receipt = _primary_receipt(
                        intent,
                        state,
                        started_at=started_at,
                        reported=True,
                        cancelled=cancelled,
                        rejection_reason=(
                            "delivery_cancelled"
                            if cancelled
                            else "invalid_or_failed_primary_stream"
                        ),
                    )
                else:
                    receipt = _primary_receipt(
                        intent,
                        state,
                        started_at=started_at,
                        reported=False,
                        cancelled=cancelled,
                    )
                await ledger.settle(receipt)
        raise


async def _stream_turn(
    session: Session,
    req: ProviderRequest,
    *,
    state: _PrimaryStreamState | None = None,
    stage_partials: bool = False,
) -> AsyncGenerator[
    PartialAssistantEvent | _PrimaryPartial | AssistantAssembly | _ParsedPrimaryAssembly,
    None,
]:
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

    def partial(delta: dict[str, Any]) -> PartialAssistantEvent | _PrimaryPartial:
        event = PartialAssistantEvent(delta=delta)
        return _PrimaryPartial(event) if stage_partials else event

    async with _provider_stream_context(agent.provider.stream(req)) as provider_stream:
        async for event in provider_stream:
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
                if not isinstance(model, str) or not model.strip():
                    raise protocol_error("message_start.model must be a nonblank string")
                if state is not None:
                    state.actual_model = model
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
                    yield partial({"kind": "text", "text": text})
            elif typ == "thinking_delta":
                flush_text()
                if not isinstance(event.get("text"), str):
                    raise protocol_error("thinking_delta.text must be a string")
                text = cast(str, event["text"])
                thinking_buf.append(text)
                signature = event.get("signature", thinking_sig)
                thinking_sig = signature if isinstance(signature, str) else thinking_sig
                if stream_partials:
                    yield partial({"kind": "thinking", "text": text})
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
                    yield partial(
                        {
                            "kind": "tool_use_input",
                            "tool_use_id": tool_id,
                            "json_delta": json_delta,
                        }
                    )
            elif typ == "tool_use_end":
                tool_id = event.get("id")
                if not isinstance(tool_id, str) or tool_id not in tool_meta:
                    raise protocol_error(
                        f"tool_use_end references unknown or closed id {tool_id!r}"
                    )
                meta = tool_meta.pop(tool_id)
                raw = "".join(tool_inputs.pop(tool_id))
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError as exc:
                    if state is None:
                        raise protocol_error(
                            f"tool {tool_id!r} input is invalid JSON: {exc}"
                        ) from exc
                    state.native_failure = state.native_failure or "malformed_native_tool_arguments"
                else:
                    if not isinstance(parsed, dict):
                        if state is None:
                            raise protocol_error(
                                f"tool {tool_id!r} input must decode to a JSON object"
                            )
                        state.native_failure = (
                            state.native_failure or "nonobject_native_tool_arguments"
                        )
                    else:
                        content.append(ToolUseBlock(id=meta[0], name=meta[1], input=parsed))
            elif typ == "message_end":
                if tool_meta:
                    raise protocol_error(
                        "message_end arrived before tool_use_end for "
                        + ", ".join(sorted(tool_meta))
                    )
                raw_usage = event.get("usage")
                if not isinstance(raw_usage, Usage):
                    raise protocol_error("message_end.usage must be a Usage instance")
                try:
                    owned_usage = _copy_usage(raw_usage)
                except _ModelCallError as exc:
                    raise protocol_error(
                        "message_end.usage must contain nonnegative integer buckets"
                    ) from exc
                if state is not None:
                    # Preserve observed spend before subsequent end-shape validation.
                    state.usage = owned_usage
                raw_stop = event.get("stop_reason")
                if not isinstance(raw_stop, str) or raw_stop not in _STOP_REASONS:
                    raise protocol_error(f"invalid message_end.stop_reason {raw_stop!r}")
                flush_text()
                flush_thinking()
                stop_reason = cast(StopReason, raw_stop)
                if state is not None:
                    state.stop_reason = stop_reason
                usage = owned_usage
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
    if (state is None or state.native_failure is None) and (
        stop_reason == "tool_use" and not complete_tool_calls
    ):
        raise protocol_error("stop_reason='tool_use' requires at least one complete tool call")
    if (state is None or state.native_failure is None) and (
        stop_reason != "tool_use" and complete_tool_calls
    ):
        raise protocol_error(
            f"stream completed tool calls but stop_reason was {stop_reason!r}, not 'tool_use'"
        )

    message = Message(role="assistant", content=content, provider_metadata=metadata)
    assembly = AssistantAssembly(message=message, stop_reason=stop_reason, usage=usage)
    if state is not None:
        state.message = message
    if state is None:
        yield assembly
    else:
        yield _ParsedPrimaryAssembly(assembly=assembly, state=state)
