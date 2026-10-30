"""Private OpenTelemetry API instrumentation; SDK/exporter lifecycle belongs to hosts.

Keep experimental GenAI attribute names here so semantic convention updates are
local. Services are agent-scoped; nothing here installs a process-global provider.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable
from dataclasses import asdict, is_dataclass
from functools import wraps
from time import perf_counter
from typing import TYPE_CHECKING, Any, TypeVar

from opentelemetry import metrics, trace

if TYPE_CHECKING:
    from .kernel.context import Context

OPERATION = "gen_ai.operation.name"
PROVIDER = "gen_ai.provider.name"
MODEL = "gen_ai.request.model"
RESPONSE_MODEL = "gen_ai.response.model"
FINISH_REASONS = "gen_ai.response.finish_reasons"
INPUT_TOKENS = "gen_ai.usage.input_tokens"
OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
CACHE_READ_TOKENS = "gen_ai.usage.cache_read.input_tokens"
CACHE_CREATION_TOKENS = "gen_ai.usage.cache_creation.input_tokens"
CONVERSATION_ID = "gen_ai.conversation.id"
TOOL_NAME = "gen_ai.tool.name"
TOOL_CALL_ID = "gen_ai.tool.call.id"
INPUT_MESSAGES = "gen_ai.input.messages"
OUTPUT_MESSAGES = "gen_ai.output.messages"
TOOL_ARGUMENTS = "gen_ai.tool.call.arguments"
TOOL_RESULT = "gen_ai.tool.call.result"
TOKEN_TYPE = "gen_ai.token.type"
RUN_ID = "linch.run_id"
TURN_INDEX = "linch.turn_index"
RESUMED = "linch.resumed"
STOP_REASON = "linch.stop_reason"
COMPACTION_STRATEGY = "linch.compaction.strategy"
ERROR_TYPE = "error.type"
STORE_OP = "op"
TOKEN_USAGE = "gen_ai.client.token.usage"
OPERATION_DURATION = "gen_ai.client.operation.duration"
STORE_DURATION = "linch.store.duration"

_T = TypeVar("_T")


def set_error(span: trace.Span, error: BaseException | str) -> None:
    """Record error identity only: exception messages may contain private content."""
    span.set_attribute(ERROR_TYPE, error if isinstance(error, str) else type(error).__name__)
    span.set_status(trace.StatusCode.ERROR)


async def in_span(iterator: AsyncIterator[_T], span: trace.Span) -> AsyncGenerator[_T, None]:
    """Activate only while advancing/closing, never across a consumer yield."""
    try:
        while True:
            with trace.use_span(
                span, end_on_exit=False, record_exception=False, set_status_on_exception=False
            ):
                try:
                    item = await anext(iterator)
                except StopAsyncIteration:
                    return
            yield item
    except BaseException as exc:
        if not isinstance(exc, GeneratorExit):
            set_error(span, exc)
        raise
    finally:
        try:
            with trace.use_span(
                span, end_on_exit=False, record_exception=False, set_status_on_exception=False
            ):
                close = getattr(iterator, "aclose", None)
                if close is not None:
                    await close()
        finally:
            span.end()


class Telemetry:
    """Agent-scoped API instruments resolved through the agent's Context."""

    def __init__(self, context: Context) -> None:
        self.tracer: trace.Tracer = context.get("tracer") or trace.get_tracer(
            "linch", tracer_provider=context.get("tracer_provider")
        )
        meter: metrics.Meter = context.get("meter") or metrics.get_meter(
            "linch", meter_provider=context.get("meter_provider")
        )
        self.tokens = meter.create_histogram(TOKEN_USAGE, unit="{token}")
        self.duration = meter.create_histogram(OPERATION_DURATION, unit="s")
        self.store_duration = meter.create_histogram(STORE_DURATION, unit="s")

    async def store_call(self, op: str, awaitable: Awaitable[_T]) -> _T:
        started = perf_counter()
        attributes = {STORE_OP: op}
        try:
            return await awaitable
        except BaseException as exc:
            attributes[ERROR_TYPE] = type(exc).__name__
            raise
        finally:
            self.store_duration.record(perf_counter() - started, attributes)


def telemetry_for(agent: Any) -> Telemetry:
    context = agent.context
    telemetry = context.get("_linch_telemetry")
    if telemetry is None:
        telemetry = Telemetry(context)
        context.register("_linch_telemetry", telemetry)
    return telemetry


def attributes(
    session: Any, operation: str, *, model: str | None = None, agent: Any = None
) -> dict[str, Any]:
    agent = session.agent if agent is None else agent
    provider_id = agent.provider.id
    provider = {"openai-chat": "openai", "openai-responses": "openai", "gemini": "gcp.gemini"}.get(
        provider_id, provider_id
    )
    state = getattr(session, "_telemetry_run", None)
    attrs = {
        OPERATION: operation,
        PROVIDER: provider,
        MODEL: model or getattr(session, "active_model", None) or agent.model,
    }
    if session is not None:
        attrs[CONVERSATION_ID] = session.id
    if state is not None:
        attrs[RUN_ID] = state.run_id
        if state.turn_index is not None:
            attrs[TURN_INDEX] = state.turn_index
    return attrs


def parent_context(session: Any) -> Any:
    state = getattr(session, "_telemetry_run", None)
    current = trace.get_current_span()
    if state is not None and current is state.span and state.step is not None:
        return trace.set_span_in_context(state.step)
    return None


def capture_content() -> bool:
    return os.getenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "").lower() == "true"


def _json(value: Any) -> str:
    return json.dumps(
        value,
        default=lambda obj: (
            asdict(obj) if is_dataclass(obj) and not isinstance(obj, type) else str(obj)
        ),
    )


def set_usage(span: trace.Span, usage: Any) -> None:
    for key, value in (
        (INPUT_TOKENS, usage.input_tokens),
        (OUTPUT_TOKENS, usage.output_tokens),
        (CACHE_READ_TOKENS, usage.cache_read_tokens),
        (CACHE_CREATION_TOKENS, usage.cache_creation_tokens),
    ):
        span.set_attribute(key, value)


class _RunTrace:
    def __init__(self, span: trace.Span, run_id: str) -> None:
        self.span = span
        self.run_id = run_id
        self.step: trace.Span | None = None
        self.turn_index: int | None = None
        self.error: str | None = None

    def observe(self, event: Any) -> None:
        if event.type == "error":
            error = event.error.get("name", "run_error")
            self.error = error
            set_error(self.span, error)
            if self.step is not None:
                set_error(self.step, error)
        elif event.type == "result":
            set_usage(self.span, event.total_usage)
            self.span.set_attribute(STOP_REASON, event.stop_reason)
            if event.subtype != "success" and self.error is None:
                set_error(self.span, event.subtype)
            if self.span.is_recording() and capture_content():
                self.span.set_attribute(OUTPUT_MESSAGES, _json(event.final_text))


def trace_run(function: Any) -> Any:
    @wraps(function)
    async def wrapped(session: Any, prompt: str, opts: Any, **kwargs: Any) -> AsyncIterator[Any]:
        telemetry = telemetry_for(session.agent)
        attrs = attributes(session, "invoke_agent")
        attrs[RUN_ID] = kwargs["run_id"]
        attrs[RESUMED] = kwargs["resume_checkpoint"] is not None
        span = telemetry.tracer.start_span("invoke_agent", attributes=attrs)
        if span.is_recording() and capture_content():
            span.set_attribute(INPUT_MESSAGES, _json(prompt))
        previous = getattr(session, "_telemetry_run", None)
        state = _RunTrace(span, kwargs["run_id"])
        session._telemetry_run = state
        iterator = in_span(function(session, prompt, opts, **kwargs), span)
        try:
            async for event in iterator:
                state.observe(event)
                yield event
        finally:
            try:
                await iterator.aclose()
            finally:
                if state.step is not None:
                    state.step.end()
                session._telemetry_run = previous

    return wrapped


def start_step(session: Any, turn_index: int) -> None:
    state = getattr(session, "_telemetry_run", None)
    if state is not None:
        state.turn_index = turn_index
        state.step = telemetry_for(session.agent).tracer.start_span(
            "step",
            context=trace.set_span_in_context(state.span),
            attributes=attributes(session, "step"),
        )


def end_step(session: Any) -> None:
    state = getattr(session, "_telemetry_run", None)
    if state is not None and state.step is not None:
        state.step.end()
        state.step = None


async def chat_stream(
    session: Any, request: Any, iterator: AsyncIterator[_T], *, agent: Any = None
) -> AsyncGenerator[_T, None]:
    """One physical delivery, including normalized parsing and auxiliary calls."""
    if session is None and agent is None:
        async for item in iterator:
            yield item
        return
    agent = session.agent if agent is None else agent
    telemetry = telemetry_for(agent)
    attrs = attributes(session, "chat", model=request.model, agent=agent)
    span = telemetry.tracer.start_span(
        f"chat {request.model}",
        context=parent_context(session),
        kind=trace.SpanKind.CLIENT,
        attributes=attrs,
    )
    capture = span.is_recording() and capture_content()
    if capture:
        span.set_attribute(INPUT_MESSAGES, _json(request.messages))
    usage = None
    error = None
    output: list[Any] = []
    started = perf_counter()
    traced = in_span(iterator, span)
    try:
        async for item in traced:
            assembly: Any = getattr(item, "assembly", item)
            if hasattr(assembly, "usage") and hasattr(assembly, "message"):
                usage = assembly.usage
                span.set_attribute(FINISH_REASONS, [assembly.stop_reason])
                if capture:
                    span.set_attribute(OUTPUT_MESSAGES, _json([assembly.message]))
            elif isinstance(item, dict):
                if item.get("type") == "message_start":
                    span.set_attribute(RESPONSE_MODEL, item.get("model", request.model))
                elif item.get("type") == "message_end":
                    usage = item.get("usage")
                    span.set_attribute(FINISH_REASONS, [item.get("stop_reason", "unknown")])
                    if capture:
                        span.set_attribute(OUTPUT_MESSAGES, _json(output))
                elif capture:
                    output.append(item)
            if usage is not None:
                set_usage(span, usage)
            yield item
    except BaseException as exc:
        error = type(exc).__name__
        state = getattr(session, "_telemetry_run", None)
        if state is not None and state.step is not None:
            set_error(state.step, exc)
        raise
    finally:
        # Raw auxiliary streams have no assembled Message; capture their normalized
        # content only behind the same opt-in as primary messages and tool bodies.
        await traced.aclose()
        metric_attrs = {OPERATION: "chat", PROVIDER: attrs[PROVIDER], MODEL: request.model}
        if error is not None:
            metric_attrs[ERROR_TYPE] = error
        telemetry.duration.record(perf_counter() - started, metric_attrs)
        if usage is not None:
            for kind, count in (("input", usage.input_tokens), ("output", usage.output_tokens)):
                telemetry.tokens.record(count, {**metric_attrs, TOKEN_TYPE: kind})


def trace_chat(function: Any) -> Any:
    @wraps(function)
    async def wrapped(session: Any, request: Any, **kwargs: Any) -> AsyncIterator[Any]:
        iterator = chat_stream(session, request, function(session, request, **kwargs))
        try:
            async for item in iterator:
                yield item
        finally:
            await iterator.aclose()

    return wrapped


def trace_compaction(function: Any) -> Any:
    @wraps(function)
    async def wrapped(session: Any, agent: Any, signal: Any, strategy: Any, **kwargs: Any) -> Any:
        attrs = attributes(session, "compaction", model=kwargs.get("model"))
        attrs[COMPACTION_STRATEGY] = strategy.id
        span = telemetry_for(agent).tracer.start_span(
            "compaction", context=parent_context(session), attributes=attrs
        )
        with trace.use_span(
            span, end_on_exit=True, record_exception=False, set_status_on_exception=False
        ):
            try:
                return await function(session, agent, signal, strategy, **kwargs)
            except BaseException as exc:
                set_error(span, exc)
                raise

    return wrapped


async def store_call(session: Any, op: str, awaitable: Awaitable[_T]) -> _T:
    if session is None:
        return await awaitable
    return await telemetry_for(session.agent).store_call(op, awaitable)


def trace_tool(function: Any) -> Any:
    @wraps(function)
    async def wrapped(
        call: Any, decision: Any, agent: Any, session: Any, signal: Any, **kwargs: Any
    ) -> Any:
        attrs = {
            OPERATION: "execute_tool",
            CONVERSATION_ID: session.id,
            TOOL_NAME: call.block.name,
            TOOL_CALL_ID: call.id,
        }
        run_id = kwargs.get("run_id") or session.active_run_id
        if run_id is not None:
            attrs[RUN_ID] = run_id
        turn_index = kwargs.get("turn_index")
        if turn_index is not None:
            attrs[TURN_INDEX] = turn_index
        span = telemetry_for(agent).tracer.start_span(
            f"execute_tool {attrs[TOOL_NAME]}", context=parent_context(session), attributes=attrs
        )
        capture = span.is_recording() and capture_content()
        if capture:
            span.set_attribute(
                TOOL_ARGUMENTS, _json(getattr(decision, "updated_input", None) or call.input)
            )
        with trace.use_span(
            span, end_on_exit=True, record_exception=False, set_status_on_exception=False
        ):
            try:
                outcome = await function(call, decision, agent, session, signal, **kwargs)
                if outcome.block.is_error:
                    set_error(span, outcome.error_type or "tool_error")
                if capture:
                    span.set_attribute(TOOL_RESULT, str(outcome.block.content))
                return outcome
            except BaseException as exc:
                set_error(span, exc)
                raise

    return wrapped


class _TelemetryProvider:
    """Bind standalone compaction provider I/O to its agent's instruments."""

    def __init__(self, session: Any, provider: Any) -> None:
        self._session = session
        self._provider = provider

    def __getattr__(self, name: str) -> Any:
        return getattr(self._provider, name)

    def stream(self, request: Any) -> AsyncGenerator[Any, None]:
        return chat_stream(self._session, request, self._provider.stream(request))
