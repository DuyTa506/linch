"""Private OpenTelemetry API instrumentation; SDK/exporter lifecycle belongs to hosts.

Keep experimental GenAI attribute names here so semantic convention updates are
local. Services are agent-scoped; nothing here installs a process-global provider.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncGenerator, Awaitable
from dataclasses import asdict, is_dataclass
from functools import wraps
from time import perf_counter
from typing import TYPE_CHECKING, Any, TypeVar

from opentelemetry import metrics, trace

try:  # Private API; without it the default provider counts as active (slower, still correct).
    from opentelemetry.metrics._internal import _ProxyMeter, _ProxyMeterProvider
except ImportError:  # pragma: no cover
    _ProxyMeter = metrics.NoOpMeter
    _ProxyMeterProvider = metrics.NoOpMeterProvider

from .types import Usage

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


def in_span(
    iterator: Any,
    span: trace.Span,
    *,
    end_on_exit: bool = True,
    session: Any = None,
    run_id: str | None = None,
) -> Any:
    """Activate valid contexts only; invalid spans return the exact raw iterator."""
    if not span.get_span_context().is_valid:
        return iterator

    async def activated() -> AsyncGenerator[Any, None]:
        previous = getattr(session, "_telemetry_run", None)
        run = _RunTrace(span, run_id) if session is not None and run_id is not None else None
        if run is not None and session is not None:
            assert run_id is not None
            session._telemetry_run = run
            telemetry_for(session.agent)._runs[(session.id, run_id)] = run
        try:
            while True:
                with trace.use_span(
                    span, end_on_exit=False, record_exception=False, set_status_on_exception=False
                ):
                    try:
                        item = await anext(iterator)
                    except StopAsyncIteration:
                        return
                if run is not None:
                    run.observe(item)
                yield item
        except BaseException as exc:
            if not isinstance(exc, GeneratorExit):
                set_error(span, exc)
                if run is not None and run.step is not None:
                    set_error(run.step, exc)
            raise
        finally:
            try:
                with trace.use_span(
                    span, end_on_exit=False, record_exception=False, set_status_on_exception=False
                ):
                    close = getattr(iterator, "aclose", None)
                    if close is not None:
                        await close()
            except BaseException as exc:
                set_error(span, exc)
                if run is not None and run.step is not None:
                    set_error(run.step, exc)
                raise
            finally:
                try:
                    if run is not None and run.step is not None:
                        run.step.end()
                finally:
                    if run is not None and session is not None:
                        for tool_span, _capture in tuple(run.tool_spans.values()):
                            if tool_span.is_recording():
                                tool_span.end()
                        run.tool_spans.clear()
                        run.closed = True
                        pending = tuple(
                            task
                            for task in getattr(session, "background_tasks", ())
                            if not task.done()
                        )
                        if pending:
                            for task in pending:
                                add_done_callback = getattr(task, "add_done_callback", None)
                                if callable(add_done_callback):
                                    add_done_callback(
                                        lambda _done, *, _session=session, _run=run: (
                                            _release_closed_run(_session, _run)
                                        )
                                    )
                        else:
                            _release_closed_run(session, run)
                        session._telemetry_run = previous
                    if end_on_exit:
                        span.end()

    return activated()


class Telemetry:
    """Agent-scoped API instruments resolved through the agent's Context."""

    def __init__(self, context: Context) -> None:
        self._context = context
        self._bindings: tuple[Any, ...] | None = None
        self._runs: dict[tuple[str, str], Any] = {}
        self._refresh()

    def _refresh(self) -> None:
        context = self._context
        tracer = context.get("tracer")
        tracer_provider = context.get("tracer_provider")
        meter = context.get("meter")
        meter_provider = context.get("meter_provider")
        global_tracer = (
            trace.get_tracer_provider()
            if isinstance(tracer, trace.ProxyTracer)
            or (
                tracer is None
                and (
                    tracer_provider is None
                    or isinstance(tracer_provider, trace.ProxyTracerProvider)
                )
            )
            else None
        )
        global_meter = (
            metrics.get_meter_provider()
            if isinstance(meter, _ProxyMeter)
            or (
                meter is None
                and (meter_provider is None or isinstance(meter_provider, _ProxyMeterProvider))
            )
            else None
        )
        bindings = (tracer, tracer_provider, meter, meter_provider, global_tracer, global_meter)
        if self._bindings is not None and all(
            current is previous for current, previous in zip(bindings, self._bindings, strict=True)
        ):
            return
        self._bindings = bindings
        self.tracer: trace.Tracer = (
            tracer
            if tracer is not None
            else trace.get_tracer("linch", tracer_provider=tracer_provider)
        )
        resolved_meter = (
            meter
            if meter is not None
            else metrics.get_meter(
                "linch",
                meter_provider=(
                    global_meter
                    if isinstance(meter_provider, _ProxyMeterProvider)
                    else meter_provider
                ),
            )
        )
        self.tokens = resolved_meter.create_histogram(TOKEN_USAGE, unit="{token}")
        self.duration = resolved_meter.create_histogram(OPERATION_DURATION, unit="s")
        self.store_duration = resolved_meter.create_histogram(STORE_DURATION, unit="s")

    @property
    def tracing_enabled(self) -> bool:
        if isinstance(self.tracer, trace.NoOpTracer):
            return False
        if isinstance(self.tracer, trace.ProxyTracer):
            return not isinstance(
                trace.get_tracer_provider(), trace.NoOpTracerProvider | trace.ProxyTracerProvider
            )
        return True

    @property
    def metrics_enabled(self) -> bool:
        meter = self._context.get("meter")
        if meter is not None and not isinstance(meter, _ProxyMeter):
            return not isinstance(meter, metrics.NoOpMeter)
        provider = self._context.get("meter_provider")
        if provider is None or isinstance(provider, _ProxyMeterProvider) or meter is not None:
            provider = metrics.get_meter_provider()
        # The API has no instrument enabled flag. Its deferred default provider
        # is inactive until a host installs one; recheck so late setup still works.
        return not isinstance(provider, metrics.NoOpMeterProvider | _ProxyMeterProvider)

    async def store_call(self, op: str, awaitable: Awaitable[_T]) -> _T:
        if not self.metrics_enabled:
            return await awaitable
        started = perf_counter()
        duration = self.store_duration
        attributes = {STORE_OP: op}
        try:
            return await awaitable
        except BaseException as exc:
            attributes[ERROR_TYPE] = type(exc).__name__
            raise
        finally:
            duration.record(perf_counter() - started, attributes)


def telemetry_for(agent: Any) -> Telemetry:
    context = agent.context
    telemetry = context.get("_linch_telemetry")
    if telemetry is None or telemetry._context is not context:
        telemetry = Telemetry(context)
        context.register("_linch_telemetry", telemetry)
    else:
        telemetry._refresh()
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
        self.chat_error: str | None = None
        self.tool_spans: dict[str, tuple[trace.Span, bool]] = {}
        self.prompt_captured = False
        self.closed = False

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
                if self.step is not None:
                    set_error(self.step, event.subtype)
            if self.span.is_recording() and capture_content():
                self.span.set_attribute(OUTPUT_MESSAGES, _json(event.final_text))


def trace_run(function: Any) -> Any:
    @wraps(function)
    def wrapped(session: Any, prompt: str, opts: Any, **kwargs: Any) -> Any:
        span = kwargs.pop("_trace_span", None)
        if span is None:
            span = start_run_span(
                session,
                prompt,
                kwargs["run_id"],
                resumed=kwargs["resume_checkpoint"] is not None,
            )
        return in_span(
            function(session, prompt, opts, **kwargs),
            span,
            session=session,
            run_id=kwargs["run_id"],
        )

    return wrapped


def start_run_span(session: Any, prompt: str, run_id: str, *, resumed: bool = False) -> Any:
    """Start the run span before durable creation so its trace can be stored.

    Args:
        session: Session owning the run.
        prompt: Initial prompt used for optional content capture.
        run_id: Stable identifier for the run.
        resumed: Whether this is a resumed run.

    Returns:
        The span used for the run's instrumentation boundary.
    """
    telemetry = telemetry_for(session.agent)
    if not telemetry.tracing_enabled:
        return trace.INVALID_SPAN
    attrs = attributes(session, "invoke_agent")
    attrs[RUN_ID] = run_id
    attrs[RESUMED] = resumed
    span = telemetry.tracer.start_span("invoke_agent", attributes=attrs)
    return span


def record_prompt_capture(session: Any, prompt: str) -> None:
    """Refresh run-span prompt content after UserPromptSubmit hooks run.

    Args:
        session: Session whose active run span receives the prompt.
        prompt: Hook-processed prompt text.
    """
    state = getattr(session, "_telemetry_run", None)
    if (
        state is not None
        and not state.prompt_captured
        and state.span.is_recording()
        and capture_content()
    ):
        state.span.set_attribute(INPUT_MESSAGES, _json(prompt))
        state.prompt_captured = True


def start_step(session: Any, turn_index: int) -> None:
    state = getattr(session, "_telemetry_run", None)
    if state is not None:
        state.chat_error = None
        state.turn_index = turn_index
        telemetry = telemetry_for(session.agent)
        if not telemetry.tracing_enabled:
            return
        state.step = telemetry.tracer.start_span(
            "step",
            context=trace.set_span_in_context(state.span),
            attributes=attributes(session, "step"),
        )


def end_step(session: Any) -> None:
    state = getattr(session, "_telemetry_run", None)
    if state is not None and state.step is not None:
        if state.chat_error is not None:
            set_error(state.step, state.chat_error)
        state.step.end()
        state.step = None
        state.chat_error = None


class ChatCall:
    """Call-boundary tracing and metrics; consumers own their existing stream loop."""

    def __init__(self, session: Any, request: Any, state: Any = None, *, agent: Any = None):
        self.session, self.state = session, state
        self.usage: Usage | None = None
        self.actual_model = ""
        self.stop_reason: str | None = None
        self.message: Any = None
        self.finished = False
        self.telemetry = telemetry_for(agent or session.agent) if agent or session else None
        self.attrs = (
            attributes(session, "chat", model=request.model, agent=agent) if self.telemetry else {}
        )
        self.span = (
            self.telemetry.tracer.start_span(
                f"chat {request.model}",
                context=parent_context(session),
                kind=trace.SpanKind.CLIENT,
                attributes=self.attrs,
            )
            if self.telemetry is not None and self.telemetry.tracing_enabled
            else trace.INVALID_SPAN
        )
        self.capture = self.span.is_recording() and capture_content()
        if self.capture:
            self.span.set_attribute(INPUT_MESSAGES, _json(request.messages))
        self.instruments = (
            (self.telemetry.duration, self.telemetry.tokens) if self.telemetry is not None else None
        )
        self.record_metrics = self.telemetry is not None and self.telemetry.metrics_enabled
        self.started = perf_counter() if self.record_metrics else 0

    def __enter__(self) -> ChatCall:
        return self

    def __exit__(self, exc_type: Any, error: BaseException | None, tb: Any) -> None:
        self.finish(error)

    def finish(self, error: BaseException | None = None) -> None:
        if self.finished:
            return
        self.finished = True
        if self.telemetry is None:
            return
        run = getattr(self.session, "_telemetry_run", None)
        if run is not None and run.step is not None:
            # Retries own chat errors; only the last attempt determines the
            # step's status when the loop closes it.
            run.chat_error = (
                type(error).__name__
                if error is not None and not isinstance(error, GeneratorExit)
                else None
            )
        if not self.record_metrics and not self.span.is_recording():
            self.span.end()
            return
        result = self.state if self.state is not None else self
        usage = result.usage
        attrs = {OPERATION: "chat", PROVIDER: self.attrs[PROVIDER], MODEL: self.attrs[MODEL]}
        try:
            if error is not None and not isinstance(error, GeneratorExit):
                attrs[ERROR_TYPE] = type(error).__name__
                set_error(self.span, error)
            if self.span.is_recording():
                if result.actual_model:
                    self.span.set_attribute(RESPONSE_MODEL, result.actual_model)
                reason = getattr(result, "stop_reason", None)
                if reason:
                    self.span.set_attribute(FINISH_REASONS, [reason])
                if usage is not None:
                    set_usage(self.span, usage)
                if self.capture:
                    content = getattr(result, "message", None)
                    if content is None:
                        content = getattr(result, "text", b"")
                        if isinstance(content, bytes | bytearray):
                            content = content.decode("utf-8")
                    self.span.set_attribute(OUTPUT_MESSAGES, _json(content))
            if self.record_metrics and self.instruments is not None:
                duration, tokens = self.instruments
                duration.record(perf_counter() - self.started, attrs)
                if usage is not None:
                    tokens.record(usage.input_tokens, {**attrs, TOKEN_TYPE: "input"})
                    tokens.record(usage.output_tokens, {**attrs, TOKEN_TYPE: "output"})
        finally:
            self.span.end()


def trace_compaction(function: Any) -> Any:
    @wraps(function)
    async def wrapped(session: Any, agent: Any, signal: Any, strategy: Any, **kwargs: Any) -> Any:
        telemetry = telemetry_for(agent)
        if not telemetry.tracing_enabled:
            return await function(session, agent, signal, strategy, **kwargs)
        attrs = attributes(session, "compaction", model=kwargs.get("model"))
        attrs[COMPACTION_STRATEGY] = strategy.id
        span = telemetry.tracer.start_span(
            "compaction", context=parent_context(session), attributes=attrs
        )
        if not span.get_span_context().is_valid:
            return await function(session, agent, signal, strategy, **kwargs)
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
        telemetry = telemetry_for(agent)
        if not telemetry.tracing_enabled:
            return await function(call, decision, agent, session, signal, **kwargs)
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
        span = telemetry.tracer.start_span(
            f"execute_tool {attrs[TOOL_NAME]}", context=parent_context(session), attributes=attrs
        )
        if not span.get_span_context().is_valid:
            return await function(call, decision, agent, session, signal, **kwargs)
        capture = span.is_recording() and capture_content()
        if capture:
            span.set_attribute(
                TOOL_ARGUMENTS, _json(getattr(decision, "updated_input", None) or call.input)
            )
        run_id = kwargs.get("run_id") or getattr(call, "_run_id", None)
        run = _run_trace_for(session, run_id)
        defer_result = run is not None and capture
        with trace.use_span(
            span,
            end_on_exit=not defer_result,
            record_exception=False,
            set_status_on_exception=False,
        ):
            try:
                outcome = await function(call, decision, agent, session, signal, **kwargs)
                if outcome.block.is_error:
                    set_error(span, outcome.error_type or "tool_error")
                if capture and not defer_result:
                    span.set_attribute(TOOL_RESULT, str(outcome.block.content))
                if run is not None and defer_result:
                    run.tool_spans[call.id] = (span, capture)
                return outcome
            except BaseException as exc:
                set_error(span, exc)
                if defer_result:
                    span.end()
                raise

    return wrapped


def _run_trace_for(session: Any, run_id: str | None = None) -> Any:
    current = getattr(session, "_telemetry_run", None)
    if current is not None and (run_id is None or current.run_id == run_id):
        return current
    resolved_run_id = run_id or getattr(session, "active_run_id", None)
    if resolved_run_id is None:
        return current
    telemetry = telemetry_for(session.agent)
    return telemetry._runs.get((session.id, resolved_run_id))


def _release_closed_run(session: Any, state: Any) -> None:
    if not state.closed or state.tool_spans:
        return
    if any(not task.done() for task in getattr(session, "background_tasks", ())):
        return
    telemetry_for(session.agent)._runs.pop((session.id, state.run_id), None)


def record_tool_result(
    session: Any, tool_use_id: str, result: Any, *, run_id: str | None = None
) -> None:
    """Record and close a tool span after PostToolUse has finalized its result.

    Args:
        session: Session owning the active tool span.
        tool_use_id: Provider tool-call identifier used to find the span.
        result: Final hook-processed tool result content.
        run_id: Optional originating run id for detached tool work.
    """
    state = _run_trace_for(session, run_id)
    if state is None:
        return
    span_info = state.tool_spans.pop(tool_use_id, None)
    if span_info is None:
        _release_closed_run(session, state)
        return
    span, capture = span_info
    if capture and span.is_recording():
        span.set_attribute(TOOL_RESULT, str(result))
    span.end()
    _release_closed_run(session, state)


class _TelemetryProvider:
    """Bind standalone compaction provider I/O to its agent's instruments."""

    def __init__(self, session: Any, provider: Any) -> None:
        self._session = session
        self._provider = provider

    def __getattr__(self, name: str) -> Any:
        return getattr(self._provider, name)

    def stream(self, request: Any) -> Any:
        iterator = self._provider.stream(request)
        telemetry = telemetry_for(self._session.agent)
        if not telemetry.metrics_enabled and not telemetry.tracing_enabled:
            return iterator

        async def activated() -> AsyncGenerator[Any, None]:
            with ChatCall(self._session, request) as chat:
                output: list[Any] = []
                if chat.capture:
                    chat.message = output
                stream = in_span(iterator, chat.span, end_on_exit=False)
                try:
                    async for event in stream:
                        kind = event.get("type")
                        if kind == "message_start":
                            chat.actual_model = event.get("model", request.model)
                        elif kind == "message_end":
                            usage = event.get("usage")
                            chat.usage = usage if isinstance(usage, Usage) else None
                            chat.stop_reason = event.get("stop_reason")
                        elif chat.capture:
                            output.append(event)
                        yield event
                finally:
                    close = getattr(stream, "aclose", None)
                    if close is not None:
                        await close()

        return activated()
