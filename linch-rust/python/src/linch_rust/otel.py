"""OpenTelemetry spans and metrics of each run, built from its event stream.

Install the extra, `pip install 'linch-rust[otel]'`, which adds
`opentelemetry-api` only: the host owns the SDK and its exporters, as with
Python Linch. Every agent `Server.open_agent` opens is then traced with the
global tracer and meter providers, or with the ones it is given
(`open_agent(..., telemetry=Telemetry(...))`). Until the host installs an
SDK provider nothing is built, and without the extra this module is never
imported.

The run happens in the server, so the spans are built here from the
envelopes as they arrive, each started at its opening event's `ts_ms` and
ended at its closing event's, as the Rust `linch-otel` crate does::

    invoke_agent                      init .. result (or suspended)
    └─ step                           one per turn, linch.turn_index
       ├─ compaction                  .. compaction
       │  └─ chat {model}             the summary call, .. its usage
       ├─ chat {model}                context_build .. assistant / error
       └─ execute_tool {tool}         tool_call_start .. tool_call_end

Each model call records `gen_ai.client.token.usage` (unit `{token}`) and
`gen_ai.client.operation.duration` (unit `s`), with the SDK's default
buckets. Span names, attribute keys and metric names are Python Linch's.
Content (the prompt, answers, tool input and results) is left out unless
`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true`.

The run span is a child of the context current when `Agent.run`,
`resume` or `approve` is called. A resumed or approved call has no `init`
event, so its spans carry no session id or turn. A run whose stream is
left before its end keeps its spans open, and they are never exported:
read every run to its end, or `Run.result()`. `Run.trace_id` is the trace
id of its spans, for log lines.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, cast

from opentelemetry import context as otel_context
from opentelemetry import metrics, trace
from opentelemetry.context import Context
from opentelemetry.metrics import Histogram, MeterProvider
from opentelemetry.trace import Span, SpanKind, Status, StatusCode, Tracer, TracerProvider
from opentelemetry.util.types import AttributeValue

from .events import (
    AssistantEvent,
    CompactionEvent,
    ContextBuildEvent,
    Envelope,
    ErrorEvent,
    Event,
    InitEvent,
    JsonObject,
    ResultEvent,
    SuspendedEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    UsageEvent,
    UserEvent,
)

try:  # The API's stand-in until a host sets a meter provider; private.
    from opentelemetry.metrics._internal import (
        _ProxyMeterProvider,  # pyright: ignore[reportPrivateUsage]
    )
except ImportError:  # pragma: no cover - an API without it counts as set
    _ProxyMeterProvider = metrics.NoOpMeterProvider

# The instrumentation scope of the tracer and meter, as in Python Linch.
SCOPE = "linch"
# Content capture is on when this variable is `true`, in any case.
CAPTURE_CONTENT_ENV = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"

OPERATION = "gen_ai.operation.name"
PROVIDER = "gen_ai.provider.name"
MODEL = "gen_ai.request.model"
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
# Rust only: Python Linch does not export cost.
COST_USD = "linch.cost_usd"
ERROR_TYPE = "error.type"

TOKEN_USAGE = "gen_ai.client.token.usage"
OPERATION_DURATION = "gen_ai.client.operation.duration"

INVOKE_AGENT = "invoke_agent"
STEP = "step"
CHAT = "chat"
COMPACTION = "compaction"
EXECUTE_TOOL = "execute_tool"

_MAX_INT = 2**63 - 1
_USAGE_KEYS = (
    (INPUT_TOKENS, "input_tokens"),
    (OUTPUT_TOKENS, "output_tokens"),
    (CACHE_READ_TOKENS, "cache_read_tokens"),
    (CACHE_CREATION_TOKENS, "cache_creation_tokens"),
)


def capture_content_from_env() -> bool:
    """Whether `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` is `true`, in any case."""
    return os.environ.get(CAPTURE_CONTENT_ENV, "").lower() == "true"


def provider_name(provider_id: str) -> str:
    """The `gen_ai.provider.name` of a provider id, as Python Linch maps it."""
    return {"openai-chat": "openai", "openai-responses": "openai", "gemini": "gcp.gemini"}.get(
        provider_id, provider_id
    )


class Telemetry:
    """The tracer and meter providers runs are traced and measured with.

    Each one absent is the global provider, read at every run, so one the
    host sets later takes effect. A global provider no host has set (the
    API's stand-in) counts as off; when both are off a run builds nothing.

    Args:
        tracer_provider: The host's tracer provider, such as an SDK
            `TracerProvider` with its exporter.
        meter_provider: The host's meter provider.
    """

    def __init__(
        self,
        *,
        tracer_provider: TracerProvider | None = None,
        meter_provider: MeterProvider | None = None,
    ) -> None:
        self._tracer_provider = tracer_provider
        self._meter_provider = meter_provider
        self._tracer: tuple[TracerProvider, Tracer] | None = None
        self._histograms: tuple[MeterProvider, _Histograms] | None = None

    def observer(
        self,
        *,
        provider: str | None = None,
        model: str | None = None,
        session_id: str | None = None,
        turn: int | None = None,
        parent: Context | None = None,
        capture_content: bool | None = None,
    ) -> RunObserver | None:
        """A fresh observer for one run, or one resume or approve of it.

        `Agent` calls this for each call with the provider and model of its
        config; a host feeding envelopes by hand can call it too.

        Args:
            provider: The Linch provider id, reported through `provider_name`.
            model: The model; a run's `init` event overrides it.
            session_id: The session, for a resumed call; `init` overrides it.
            turn: The turn a resumed call starts in; `init` starts at 0.
            parent: The context the run span is a child of; the current
                context when absent.
            capture_content: Whether to keep content on the spans;
                `capture_content_from_env()` when absent.

        Returns:
            The observer, or `None` when neither tracing nor metrics is on.
        """
        tracer = self._resolve_tracer()
        histograms = self._resolve_histograms()
        if tracer is None and histograms is None:
            return None
        return RunObserver(
            tracer if tracer is not None else trace.NoOpTracer(),
            histograms,
            parent=otel_context.get_current() if parent is None else parent,
            capture=capture_content_from_env() if capture_content is None else capture_content,
            provider=None if provider is None else provider_name(provider),
            model=model,
            session_id=session_id,
            turn=turn,
        )

    def _resolve_tracer(self) -> Tracer | None:
        tracer_provider = self._tracer_provider
        if tracer_provider is None:
            tracer_provider = trace.get_tracer_provider()
        if isinstance(tracer_provider, trace.ProxyTracerProvider | trace.NoOpTracerProvider):
            return None
        if self._tracer is None or self._tracer[0] is not tracer_provider:
            self._tracer = (tracer_provider, tracer_provider.get_tracer(SCOPE))
        return self._tracer[1]

    def _resolve_histograms(self) -> _Histograms | None:
        meter_provider = self._meter_provider
        if meter_provider is None:
            meter_provider = metrics.get_meter_provider()
        if isinstance(meter_provider, metrics.NoOpMeterProvider | _ProxyMeterProvider):
            return None
        if self._histograms is None or self._histograms[0] is not meter_provider:
            meter = meter_provider.get_meter(SCOPE)
            histograms = _Histograms(
                tokens=meter.create_histogram(TOKEN_USAGE, unit="{token}"),
                duration=meter.create_histogram(OPERATION_DURATION, unit="s"),
            )
            self._histograms = (meter_provider, histograms)
        return self._histograms[1]


@dataclass(frozen=True)
class _Histograms:
    tokens: Histogram
    duration: Histogram


class _Open:
    """A started span, the context holding it (its children's parent), and its start."""

    def __init__(
        self,
        tracer: Tracer,
        parent: Context,
        name: str,
        kind: SpanKind,
        start_ms: int,
        attributes: dict[str, AttributeValue],
    ) -> None:
        self.span: Span = tracer.start_span(
            name, context=parent, kind=kind, attributes=attributes, start_time=_ns(start_ms)
        )
        self.context = trace.set_span_in_context(self.span, parent)
        self.start_ms = start_ms

    def recording(self) -> bool:
        return self.span.is_recording()

    def set(self, key: str, value: AttributeValue) -> None:
        self.span.set_attribute(key, value)

    def set_error(self, error_type: str) -> None:
        # The error's name only: a message may hold private content.
        self.span.set_attribute(ERROR_TYPE, error_type)
        self.span.set_status(Status(StatusCode.ERROR))

    def set_usage(self, usage: JsonObject) -> None:
        for key, field in _USAGE_KEYS:
            self.span.set_attribute(key, _tokens(usage, field))

    def end(self, end_ms: int) -> None:
        # Never before the start: wall clocks can step back.
        self.span.end(end_time=_ns(max(end_ms, self.start_ms)))


class _Chat:
    """An open model call; `answered` is when its response arrived."""

    def __init__(self, open: _Open) -> None:
        self.open = open
        self.usage: JsonObject | None = None
        self.cost: float | None = None
        self.answered: int | None = None


class _Compaction:
    """An open compaction, and the last time its summary call was seen."""

    def __init__(self, open: _Open, last: int) -> None:
        self.open = open
        self.last = last


class RunObserver:
    """Builds the spans and metrics of one run, or one resume or approve of it.

    Feed it every envelope of the stream with `observe`, in order. The
    run's spans end on its `result` or `suspended` event; if the stream ends
    any other way, call `fail` with the error's type name, or `close`: each
    ends every open span at the wall clock. Events in an order no run
    produces are traced as far as they make sense and never raise; an
    envelope of another run, or anything after the end, is ignored. Made by
    `Telemetry.observer`.
    """

    def __init__(
        self,
        tracer: Tracer,
        histograms: _Histograms | None,
        *,
        parent: Context,
        capture: bool,
        provider: str | None,
        model: str | None,
        session_id: str | None,
        turn: int | None,
    ) -> None:
        self._tracer = tracer
        self._histograms = histograms
        self._parent = parent
        self._capture = capture
        self._provider = provider
        self._model = model
        self._session_id = session_id
        self._turn = turn
        self._run_id: str | None = None
        self._run: _Open | None = None
        self._trace_id: str | None = None
        self._step: _Open | None = None
        # When the next turn begins: the prompt, or the end of the last turn.
        self._turn_opened: int | None = None
        self._chat: _Chat | None = None
        self._compaction: _Compaction | None = None
        self._tools: list[tuple[str, _Open]] = []
        # Whether an `error` event already marked the run.
        self._errored = False
        self._prompt_captured = False
        self._ended = False

    @property
    def trace_id(self) -> str | None:
        """The run span's trace id, as 32 hex digits, once the first envelope
        started it; before that, the parent's, if it has one."""
        if self._trace_id is not None:
            return self._trace_id
        return _trace_id(self._parent)

    def observe(self, envelope: Envelope) -> None:
        """Trace one envelope of the run."""
        if self._ended:
            return
        if self._run_id is None:
            self._run_id = envelope.run_id
        elif envelope.run_id != self._run_id:
            return
        event, at = envelope.event, envelope.ts_ms
        if not isinstance(event, UsageEvent):
            self._close_answered_chat()
        if self._run is None:
            self._start_run(event, at)
        if isinstance(event, UserEvent):
            self._user(event, at)
        elif isinstance(event, ContextBuildEvent):
            self._context_build(at)
        elif isinstance(event, AssistantEvent):
            self._assistant(event, at)
        elif isinstance(event, UsageEvent):
            self._usage(event, at)
        elif isinstance(event, CompactionEvent):
            self._compacted(event, at)
        elif isinstance(event, ToolCallStartEvent):
            self._tool_start(event, at)
        elif isinstance(event, ToolCallEndEvent):
            self._tool_end(event, at)
        elif isinstance(event, ErrorEvent):
            self._error(event, at)
        elif isinstance(event, ResultEvent):
            self._result(event, at)
        elif isinstance(event, SuspendedEvent):
            self._close(at, None)

    def fail(self, error_type: str) -> None:
        """End the run for a stream that failed with `error_type`, a type name.

        Every open span ends now, marked with it; never pass a message.
        """
        if not self._ended:
            self._close_answered_chat()
            self._close(_now_ms(), error_type)

    def close(self) -> None:
        """End every open span now, unmarked; nothing after the run's end."""
        if not self._ended:
            self._close_answered_chat()
            self._close(_now_ms(), None)

    def _start(
        self,
        parent: Context,
        name: str,
        kind: SpanKind,
        at: int,
        attributes: dict[str, AttributeValue],
    ) -> _Open:
        return _Open(self._tracer, parent, name, kind, at, attributes)

    def _attributes(self, operation: str, with_turn: bool) -> dict[str, AttributeValue]:
        attributes: dict[str, AttributeValue] = {OPERATION: operation}
        for key, value in (
            (PROVIDER, self._provider),
            (MODEL, self._model),
            (CONVERSATION_ID, self._session_id),
            (RUN_ID, self._run_id),
        ):
            if value is not None:
                attributes[key] = value
        if with_turn and self._turn is not None:
            attributes[TURN_INDEX] = _count(self._turn)
        return attributes

    def _start_run(self, event: Event, at: int) -> None:
        resumed = True
        if isinstance(event, InitEvent):
            self._session_id = event.session_id
            self._model = event.model
            self._turn = 0
            resumed = False
        attributes = self._attributes(INVOKE_AGENT, False)
        attributes[RESUMED] = resumed
        self._run = self._start(self._parent, INVOKE_AGENT, SpanKind.INTERNAL, at, attributes)
        self._trace_id = _trace_id(self._run.context)

    def _open_step(self, at: int) -> tuple[Context, int]:
        """The open step's context and start, opening it if the turn has none."""
        if self._step is not None:
            return self._step.context, self._step.start_ms
        start = at if self._turn_opened is None else self._turn_opened
        parent = self._parent if self._run is None else self._run.context
        attributes = self._attributes(STEP, True)
        self._step = self._start(parent, STEP, SpanKind.INTERNAL, start, attributes)
        return self._step.context, start

    def _user(self, event: UserEvent, at: int) -> None:
        if event.subtype == "prompt":
            run = self._run
            if self._capture and not self._prompt_captured and run and run.recording():
                run.set(INPUT_MESSAGES, _json(_message_text(event.message)))
                self._prompt_captured = True
        elif self._step is not None:
            # Tool results (or stop feedback) end the turn.
            self._end_step(at, None)
            if self._turn is not None:
                self._turn += 1
        self._turn_opened = at

    def _error(self, event: ErrorEvent, at: int) -> None:
        name = event.error.get("name")
        error_type = name if isinstance(name, str) else "run_error"
        self._errored = True
        if self._chat is not None:
            chat, self._chat = self._chat, None
            self._finish_chat(chat, at, error_type)
        for open in (self._step, self._run):
            if open is not None:
                open.set_error(error_type)

    def _result(self, event: ResultEvent, at: int) -> None:
        failed = None
        if event.subtype != "success" and not self._errored:
            failed = event.subtype
        run = self._run
        if run is not None:
            run.set_usage(event.total_usage)
            run.set(STOP_REASON, event.stop_reason)
            if event.total_cost_usd is not None:
                run.set(COST_USD, event.total_cost_usd)
            if self._capture and run.recording():
                run.set(OUTPUT_MESSAGES, _json(event.final_text))
        self._close(at, failed)

    def _end_step(self, at: int, error: str | None) -> None:
        """End the turn's open spans, marked with `error` if set, then the step."""
        if self._chat is not None:
            chat, self._chat = self._chat, None
            self._finish_chat(chat, at, error)
        ending: list[tuple[_Open, int]] = []
        if self._compaction is not None:
            ending.append((self._compaction.open, self._compaction.last))
            self._compaction = None
        ending.extend((open, at) for _, open in self._tools)
        self._tools.clear()
        if self._step is not None:
            ending.append((self._step, at))
            self._step = None
        for open, end in ending:
            if error is not None:
                open.set_error(error)
            open.end(end)

    def _close(self, at: int, error: str | None) -> None:
        """End every open span at `at`, marked with `error` if set."""
        self._end_step(at, error)
        if self._run is not None:
            if error is not None:
                self._run.set_error(error)
            self._run.end(at)
            self._run = None
        self._ended = True

    def _context_build(self, at: int) -> None:
        # A summary call that gave no compaction ends where it was last seen.
        if self._compaction is not None:
            self._compaction.open.end(self._compaction.last)
            self._compaction = None
        if self._chat is not None:
            chat, self._chat = self._chat, None
            self._finish_chat(chat, at, None)
        step, _ = self._open_step(at)
        self._chat = self._open_chat(step, at)

    def _open_chat(self, parent: Context, at: int) -> _Chat:
        name = CHAT if self._model is None else f"{CHAT} {self._model}"
        attributes = self._attributes(CHAT, True)
        return _Chat(self._start(parent, name, SpanKind.CLIENT, at, attributes))

    def _assistant(self, event: AssistantEvent, at: int) -> None:
        chat = self._chat
        if chat is None:
            return
        chat.open.set(FINISH_REASONS, [event.stop_reason])
        if self._capture and chat.open.recording():
            chat.open.set(OUTPUT_MESSAGES, _json(event.message))
        chat.answered = at

    def _usage(self, event: UsageEvent, at: int) -> None:
        if self._chat is not None:
            self._chat.usage = event.usage
            self._chat.cost = event.cost_usd
            self._close_answered_chat()
            return
        # No primary call is open, so this is the turn's summary call.
        parent, start = self._open_compaction(at)
        self._summary_call(parent, start, at, event)
        if self._compaction is not None:
            self._compaction.last = at

    def _open_compaction(self, at: int) -> tuple[Context, int]:
        if self._compaction is not None:
            return self._compaction.open.context, self._compaction.open.start_ms
        step, start = self._open_step(at)
        attributes = self._attributes(COMPACTION, True)
        open = self._start(step, COMPACTION, SpanKind.INTERNAL, start, attributes)
        self._compaction = _Compaction(open, at)
        return open.context, start

    def _summary_call(
        self, parent: Context, start: int, end: int, usage: UsageEvent | None
    ) -> None:
        """The compaction's summary call, from the turn's start to `end`."""
        chat = self._open_chat(parent, start)
        if usage is not None:
            chat.usage = usage.usage
            chat.cost = usage.cost_usd
        self._finish_chat(chat, end, None)

    def _compacted(self, event: CompactionEvent, at: int) -> None:
        if self._compaction is None:
            # Its summary call reported no usage.
            parent, start = self._open_compaction(at)
            self._summary_call(parent, start, at, None)
        compaction = self._compaction
        if compaction is not None:
            self._compaction = None
            compaction.open.set(COMPACTION_STRATEGY, event.strategy)
            compaction.open.end(at)

    def _close_answered_chat(self) -> None:
        """Close a chat whose response arrived and whose usage did not follow."""
        chat = self._chat
        if chat is not None and chat.answered is not None:
            self._chat = None
            self._finish_chat(chat, chat.answered, None)

    def _finish_chat(self, chat: _Chat, at: int, error: str | None) -> None:
        """End the chat: its span, its duration and its token counts."""
        open = chat.open
        attributes: dict[str, AttributeValue] = {OPERATION: CHAT}
        if self._provider is not None:
            attributes[PROVIDER] = self._provider
        if self._model is not None:
            attributes[MODEL] = self._model
        if error is not None:
            open.set_error(error)
            attributes[ERROR_TYPE] = error
        if chat.usage is not None:
            open.set_usage(chat.usage)
        if chat.cost is not None:
            open.set(COST_USD, chat.cost)
        seconds = max(at - open.start_ms, 0) / 1000
        open.end(at)
        histograms = self._histograms
        if histograms is None:
            return
        histograms.duration.record(seconds, attributes)
        if chat.usage is not None:
            for kind, field in (("input", "input_tokens"), ("output", "output_tokens")):
                tagged = {**attributes, TOKEN_TYPE: kind}
                histograms.tokens.record(_tokens(chat.usage, field), tagged)

    def _tool_start(self, event: ToolCallStartEvent, at: int) -> None:
        step, _ = self._open_step(at)
        call_id = event.tool_use_id
        for index, (open_id, open) in enumerate(self._tools):
            if open_id == call_id:
                del self._tools[index]
                open.end(at)
                break
        open = self._open_tool(step, event.tool_name, call_id, at)
        if self._capture and open.recording():
            open.set(TOOL_ARGUMENTS, _json(event.input))
        self._tools.append((call_id, open))

    def _open_tool(self, parent: Context, name: str, call_id: str, at: int) -> _Open:
        attributes: dict[str, AttributeValue] = {OPERATION: EXECUTE_TOOL}
        if self._session_id is not None:
            attributes[CONVERSATION_ID] = self._session_id
        attributes[TOOL_NAME] = name
        attributes[TOOL_CALL_ID] = call_id
        if self._run_id is not None:
            attributes[RUN_ID] = self._run_id
        if self._turn is not None:
            attributes[TURN_INDEX] = _count(self._turn)
        return self._start(parent, f"{EXECUTE_TOOL} {name}", SpanKind.INTERNAL, at, attributes)

    def _tool_end(self, event: ToolCallEndEvent, at: int) -> None:
        step, _ = self._open_step(at)
        call_id = event.tool_use_id
        found = next((i for i, (open_id, _) in enumerate(self._tools) if open_id == call_id), None)
        if found is not None:
            open, error = self._tools.pop(found)[1], "tool_error"
        else:
            # Its start is in an earlier stream: the placeholder end of a
            # tool a crash interrupted.
            start = max(at - event.duration_ms, 0)
            open, error = self._open_tool(step, event.tool_name, call_id, start), "interrupted"
        if event.is_error:
            open.set_error(error)
        if self._capture and open.recording():
            open.set(TOOL_RESULT, _result_text(event.result))
        open.end(at)


def _trace_id(context: Context) -> str | None:
    """The trace id of the span in `context`, or `None` without a valid one."""
    span_context = trace.get_current_span(context).get_span_context()
    return format(span_context.trace_id, "032x") if span_context.is_valid else None


def _ns(ms: int) -> int:
    return max(ms, 0) * 1_000_000


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _count(value: int) -> int:
    return min(max(value, 0), _MAX_INT)


def _tokens(usage: JsonObject, field: str) -> int:
    value = usage.get(field, 0)
    return _count(value) if isinstance(value, int) else 0


def _json(value: Any) -> str:
    """`value` as compact JSON text, as `serde_json` writes it."""
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return ""


def _blocks(content: Any) -> list[JsonObject]:
    """The object blocks of a message's or a tool result's content."""
    items = cast(list[Any], content) if isinstance(content, list) else []
    return [cast(JsonObject, item) for item in items if isinstance(item, dict)]


def _message_text(message: JsonObject) -> str:
    """A message's text blocks, joined."""
    blocks = _blocks(message.get("content"))
    return "".join(str(block.get("text", "")) for block in blocks if block.get("type") == "text")


def _result_text(result: JsonObject) -> str:
    """A tool result as text: its text parts, or the content's JSON when it holds an image."""
    content = result.get("content")
    blocks = _blocks(content)
    if any(block.get("type") != "text" for block in blocks):
        return _json(content)
    return "".join(str(block.get("text", "")) for block in blocks)
