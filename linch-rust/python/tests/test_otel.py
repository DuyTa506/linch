"""Spans and metrics of runs, from their event streams, as `linch-otel` builds them.

Each test has its own SDK providers and passes them in: a process can set
the global providers only once, so the global default runs in a subprocess.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import HistogramDataPoint, InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode
from scripted import MODEL, config, text_turn, tool_turn

from linch_rust import (
    Allow,
    ConnectionClosed,
    Envelope,
    ErrorEvent,
    HostTool,
    Server,
    SuspendedEvent,
    tool,
)
from linch_rust.events import decode_envelope
from linch_rust.otel import RunObserver, Telemetry

SRC = Path(__file__).resolve().parents[1] / "src"
CONTENT_KEYS = (
    "gen_ai.input.messages",
    "gen_ai.output.messages",
    "gen_ai.tool.call.arguments",
    "gen_ai.tool.call.result",
)


class Sdk:
    """SDK providers whose spans and metrics stay in memory."""

    def __init__(self) -> None:
        self.exporter = InMemorySpanExporter()
        self.tracer_provider = TracerProvider()
        self.tracer_provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self.reader = InMemoryMetricReader()
        self.meter_provider = MeterProvider(metric_readers=[self.reader])
        self.telemetry = Telemetry(
            tracer_provider=self.tracer_provider, meter_provider=self.meter_provider
        )

    def observer(self) -> RunObserver:
        """An observer for hand-built streams: provider `scripted`, no content."""
        observer = self.telemetry.observer(provider="scripted", capture_content=False)
        assert observer is not None
        return observer

    def spans(self) -> list[ReadableSpan]:
        return list(self.exporter.get_finished_spans())

    def metric(self, name: str) -> Any:
        data = self.reader.get_metrics_data()
        assert data is not None
        for resource in data.resource_metrics:
            for scope in resource.scope_metrics:
                assert scope.scope.name == "linch"
                for metric in scope.metrics:
                    if metric.name == name:
                        return metric
        return None

    def points(self, name: str) -> list[HistogramDataPoint]:
        metric = self.metric(name)
        return [] if metric is None else list(metric.data.data_points)


def names(spans: Sequence[ReadableSpan]) -> list[str]:
    return sorted(s.name for s in spans)


def all_named(spans: Sequence[ReadableSpan], name: str) -> list[ReadableSpan]:
    return sorted((s for s in spans if s.name == name), key=lambda s: s.start_time or 0)


def one(spans: Sequence[ReadableSpan], name: str) -> ReadableSpan:
    [found] = all_named(spans, name)
    return found


def attr(span: ReadableSpan, key: str) -> Any:
    return (span.attributes or {}).get(key)


def is_child(child: ReadableSpan, parent: ReadableSpan) -> bool:
    assert parent.context is not None
    return child.parent is not None and child.parent.span_id == parent.context.span_id


def at(ms: int) -> int:
    return ms * 1_000_000


def ts(envelopes: Sequence[Envelope], kind: str, index: int = 0) -> int:
    return [e.ts_ms for e in envelopes if e.event.type == kind][index]


def error_type(span: ReadableSpan) -> Any:
    return attr(span, "error.type")


# Events in their wire form, for streams built by hand: run `r1` of session
# `s1` on model `m`.


def envelope(seq: int, ts_ms: int, event: dict[str, Any], run_id: str = "r1") -> Envelope:
    return decode_envelope({"v": 1, "seq": seq, "run_id": run_id, "ts_ms": ts_ms, "event": event})


def init() -> dict[str, Any]:
    return {
        "type": "init",
        "session_id": "s1",
        "run_id": "r1",
        "model": "m",
        "provider": "scripted",
        "tools": [],
    }


def prompt() -> dict[str, Any]:
    message = {"role": "user", "content": [{"type": "text", "text": "hi"}]}
    return {"type": "user", "message": message, "subtype": "prompt"}


def context_build() -> dict[str, Any]:
    return {
        "type": "context_build",
        "system_blocks": 1,
        "messages": 1,
        "budget": {},
        "metadata": {},
    }


def assistant(stop_reason: str) -> dict[str, Any]:
    message = {"role": "assistant", "content": [{"type": "text", "text": "x"}]}
    return {"type": "assistant", "message": message, "stop_reason": stop_reason}


def usage_event(input: int, output: int) -> dict[str, Any]:
    usage = {"input_tokens": input, "output_tokens": output}
    return {"type": "usage", "usage": usage, "cumulative": usage}


def tool_start(call_id: str) -> dict[str, Any]:
    return {
        "type": "tool_call_start",
        "tool_use_id": call_id,
        "tool_name": "Echo",
        "input": {},
        "summary": "Echo",
    }


def tool_end(call_id: str, is_error: bool, duration_ms: int) -> dict[str, Any]:
    result = {"tool_use_id": call_id, "content": [{"type": "text", "text": "out"}]}
    result["is_error"] = is_error
    return {
        "type": "tool_call_end",
        "tool_name": "Echo",
        "duration_ms": duration_ms,
        "result": result,
    }


def tool_results(call_id: str) -> dict[str, Any]:
    block = {"type": "tool_result", "tool_use_id": call_id, "content": []}
    return {
        "type": "user",
        "message": {"role": "user", "content": [block]},
        "subtype": "tool_result",
    }


def result(subtype: str) -> dict[str, Any]:
    zero = {"input_tokens": 0, "output_tokens": 0}
    return {
        "type": "result",
        "subtype": subtype,
        "stop_reason": "error",
        "total_usage": zero,
        "usage_complete": True,
        "duration_ms": 1,
    }


def feed(observer: RunObserver, stream: Sequence[tuple[int, dict[str, Any]]]) -> None:
    for seq, (ts_ms, event) in enumerate(stream, start=1):
        observer.observe(envelope(seq, ts_ms, event))


@tool(scope="read")
async def echo(value: str) -> str:
    """Echo the value."""
    return value


async def traced(
    sdk: Sdk, turns: list[dict[str, Any]], tools: Sequence[HostTool] = ()
) -> list[Envelope]:
    async with Server() as server:
        agent = await server.open_agent(config(turns), tools=tools, telemetry=sdk.telemetry)
        run = await agent.run("s1", "hi")
        return [envelope async for envelope in run]


@pytest.fixture(autouse=True)
def no_content_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", raising=False)


async def test_a_text_run_is_an_agent_span_over_one_step_over_one_chat() -> None:
    sdk = Sdk()
    envelopes = await traced(sdk, [text_turn("hello")])

    spans = sdk.spans()
    assert names(spans) == [f"chat {MODEL}", "invoke_agent", "step"]
    run, step, chat = one(spans, "invoke_agent"), one(spans, "step"), one(spans, f"chat {MODEL}")
    assert run.parent is None
    assert is_child(step, run) and is_child(chat, step)
    assert (run.kind, step.kind, chat.kind) == (
        SpanKind.INTERNAL,
        SpanKind.INTERNAL,
        SpanKind.CLIENT,
    )
    assert run.instrumentation_scope is not None and run.instrumentation_scope.name == "linch"

    # Each span opens and closes on its events' times.
    assert (run.start_time, run.end_time) == (
        at(ts(envelopes, "init")),
        at(ts(envelopes, "result")),
    )
    assert (step.start_time, step.end_time) == (
        at(ts(envelopes, "user")),
        at(ts(envelopes, "result")),
    )
    assert (chat.start_time, chat.end_time) == (
        at(ts(envelopes, "context_build")),
        at(ts(envelopes, "assistant")),
    )

    run_id = envelopes[0].run_id
    assert attr(run, "gen_ai.operation.name") == "invoke_agent"
    assert attr(run, "gen_ai.provider.name") == "scripted"
    assert attr(run, "gen_ai.request.model") == MODEL
    assert attr(run, "gen_ai.conversation.id") == "s1"
    assert attr(run, "linch.run_id") == run_id
    assert attr(run, "linch.resumed") is False
    assert attr(run, "linch.stop_reason") == "end_turn"
    assert attr(run, "gen_ai.usage.input_tokens") == 3
    assert attr(run, "gen_ai.usage.output_tokens") == 1
    assert attr(run, "gen_ai.usage.cache_read.input_tokens") == 0
    assert attr(run, "gen_ai.usage.cache_creation.input_tokens") == 0
    assert attr(run, "linch.turn_index") is None
    assert run.status.status_code == StatusCode.UNSET

    assert attr(step, "gen_ai.operation.name") == "step"
    assert attr(step, "linch.turn_index") == 0
    assert attr(step, "linch.run_id") == run_id

    assert attr(chat, "gen_ai.operation.name") == "chat"
    assert attr(chat, "gen_ai.conversation.id") == "s1"
    assert attr(chat, "linch.turn_index") == 0
    assert attr(chat, "gen_ai.response.finish_reasons") == ("end_turn",)
    assert attr(chat, "gen_ai.usage.input_tokens") == 3
    assert chat.status.status_code == StatusCode.UNSET and error_type(chat) is None


async def test_a_run_tells_the_trace_id_of_its_spans() -> None:
    sdk = Sdk()
    async with Server() as server:
        agent = await server.open_agent(config([text_turn("hi")]), telemetry=sdk.telemetry)
        run = await agent.run("s1", "Hi")
        before = run.trace_id
        await run.result()

    run_span = one(sdk.spans(), "invoke_agent")
    assert before is None, "no parent span, and the run span starts on its first envelope"
    assert run_span.context is not None
    assert run.trace_id == format(run_span.context.trace_id, "032x")


def test_before_its_first_envelope_an_observer_tells_its_parents_trace_id() -> None:
    sdk = Sdk()
    with sdk.tracer_provider.get_tracer("host").start_as_current_span("request") as request:
        observer = sdk.observer()
    request_trace = format(request.get_span_context().trace_id, "032x")

    assert observer.trace_id == request_trace
    observer.observe(envelope(1, 0, init()))
    assert observer.trace_id == request_trace
    assert sdk.observer().trace_id is None


async def test_an_untraced_run_has_no_trace_id() -> None:
    async with Server() as server:
        agent = await server.open_agent(config([text_turn("hi")]))
        run = await agent.run("s1", "Hi")
        await run.result()

    assert run.trace_id is None


async def test_each_model_call_records_its_tokens_and_duration() -> None:
    sdk = Sdk()
    envelopes = await traced(sdk, [text_turn("hello")])

    assert sdk.metric("gen_ai.client.token.usage").unit == "{token}"
    assert sdk.metric("gen_ai.client.operation.duration").unit == "s"
    tokens = sorted(sdk.points("gen_ai.client.token.usage"), key=lambda p: str(p.attributes))
    assert [(dict(p.attributes or {}), p.count, p.sum) for p in tokens] == [
        (
            {
                "gen_ai.operation.name": "chat",
                "gen_ai.provider.name": "scripted",
                "gen_ai.request.model": MODEL,
                "gen_ai.token.type": kind,
            },
            1,
            total,
        )
        for kind, total in (("input", 3), ("output", 1))
    ]
    [duration] = sdk.points("gen_ai.client.operation.duration")
    elapsed = ts(envelopes, "assistant") - ts(envelopes, "context_build")
    assert (duration.count, duration.sum) == (1, elapsed / 1000)
    assert len(duration.attributes or {}) == 3


async def test_tool_calls_are_spans_under_their_turn_and_each_turn_is_a_step() -> None:
    sdk = Sdk()
    turns = [tool_turn("call_1", "echo", {"value": "v"}), text_turn("done")]
    envelopes = await traced(sdk, turns, tools=[echo])

    spans = sdk.spans()
    run = one(spans, "invoke_agent")
    steps = all_named(spans, "step")
    chats = all_named(spans, f"chat {MODEL}")
    tool_span = one(spans, "execute_tool echo")
    assert (len(steps), len(chats)) == (2, 2)
    assert all(is_child(step, run) for step in steps)
    assert [attr(step, "linch.turn_index") for step in steps] == [0, 1]
    assert is_child(chats[0], steps[0]) and is_child(chats[1], steps[1])

    # The first turn ends on its tool results; the next starts there.
    results = ts(envelopes, "user", 1)
    assert steps[0].end_time == at(results) == steps[1].start_time

    assert is_child(tool_span, steps[0])
    assert tool_span.kind == SpanKind.INTERNAL
    assert (tool_span.start_time, tool_span.end_time) == (
        at(ts(envelopes, "tool_call_start")),
        at(ts(envelopes, "tool_call_end")),
    )
    assert attr(tool_span, "gen_ai.operation.name") == "execute_tool"
    assert attr(tool_span, "gen_ai.tool.name") == "echo"
    assert attr(tool_span, "gen_ai.tool.call.id") == "call_1"
    assert attr(tool_span, "gen_ai.conversation.id") == "s1"
    assert attr(tool_span, "linch.turn_index") == 0
    assert attr(tool_span, "gen_ai.provider.name") is None
    assert error_type(tool_span) is None
    assert attr(chats[0], "gen_ai.response.finish_reasons") == ("tool_use",)
    assert attr(run, "gen_ai.usage.input_tokens") == 6
    assert sdk.points("gen_ai.client.operation.duration")[0].count == 2


async def test_a_failed_provider_call_marks_the_chat_the_step_and_the_run() -> None:
    sdk = Sdk()
    failing = {"error": {"type": "invalid_request", "message": "secret detail"}}
    envelopes = await traced(sdk, [failing])

    [error] = [e.event.error for e in envelopes if isinstance(e.event, ErrorEvent)]
    spans = sdk.spans()
    chat, step, run = one(spans, f"chat {MODEL}"), one(spans, "step"), one(spans, "invoke_agent")
    assert error["name"] == error_type(chat) == error_type(step) == error_type(run)
    assert chat.end_time == at(ts(envelopes, "error"))
    assert chat.status.status_code == StatusCode.ERROR
    assert not chat.status.description, "the message stays out"
    assert attr(run, "linch.stop_reason") == "error"
    [duration] = sdk.points("gen_ai.client.operation.duration")
    assert (duration.attributes or {})["error.type"] == error["name"]
    assert sdk.points("gen_ai.client.token.usage") == []


async def test_a_suspended_run_ends_its_spans_and_its_approval_is_a_resumed_run() -> None:
    sdk = Sdk()

    @tool(scope="write")
    async def save(value: str) -> str:
        """Save the value."""
        return f"saved {value}"

    turns = [tool_turn("call_1", "save", {"value": "v"}), text_turn("done")]
    host_tracer = sdk.tracer_provider.get_tracer("host")
    async with Server() as server:
        agent = await server.open_agent(config(turns), tools=[save], telemetry=sdk.telemetry)
        first = await agent.run("s1", "save v")
        suspended = [e async for e in first]
        assert isinstance(suspended[-1].event, SuspendedEvent)
        with host_tracer.start_as_current_span("host request") as host:
            second = await agent.approve(first.id, {"call_1": Allow()})
        resumed = [e async for e in second]

    spans = sdk.spans()
    runs = all_named(spans, "invoke_agent")
    assert len(runs) == 2
    assert runs[0].end_time == at(suspended[-1].ts_ms)
    assert error_type(runs[0]) is None

    again = runs[1]
    assert attr(again, "linch.resumed") is True
    assert again.parent is not None and again.parent.span_id == host.get_span_context().span_id
    assert again.start_time == at(resumed[0].ts_ms)
    assert attr(again, "gen_ai.request.model") == MODEL, "the agent's model"
    assert attr(again, "gen_ai.conversation.id") is None, "a resumed call has no init"
    steps = [s for s in all_named(spans, "step") if is_child(s, again)]
    assert len(steps) == 2
    assert [attr(s, "linch.turn_index") for s in steps] == [None, None]
    tool_span = one(spans, "execute_tool save")
    assert is_child(tool_span, steps[0]) and error_type(tool_span) is None


async def test_content_is_left_out_by_default() -> None:
    sdk = Sdk()
    await traced(sdk, [tool_turn("call_1", "echo", {"value": "v"}), text_turn("hello")], [echo])

    spans = sdk.spans()
    assert len(spans) == 6, names(spans)
    for span in spans:
        for key in CONTENT_KEYS:
            assert attr(span, key) is None, f"{span.name} has {key}"


async def test_with_capture_on_the_prompt_answers_and_tool_io_are_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "True")
    sdk = Sdk()
    await traced(sdk, [tool_turn("call_1", "echo", {"value": "v"}), text_turn("hello")], [echo])

    spans = sdk.spans()
    run = one(spans, "invoke_agent")
    assert attr(run, "gen_ai.input.messages") == '"hi"'
    assert attr(run, "gen_ai.output.messages") == '"hello"'
    chats = all_named(spans, f"chat {MODEL}")
    answer = attr(chats[1], "gen_ai.output.messages")
    assert answer.startswith('{"role":"assistant"') and '"text":"hello"' in answer
    assert all(attr(chat, "gen_ai.input.messages") is None for chat in chats)
    tool_span = one(spans, "execute_tool echo")
    assert attr(tool_span, "gen_ai.tool.call.arguments") == '{"value":"v"}'
    assert attr(tool_span, "gen_ai.tool.call.result") == "v"


def test_each_span_starts_and_ends_on_its_own_events() -> None:
    sdk = Sdk()
    paid, ended = usage_event(10, 5), result("success")
    paid["cost_usd"] = 0.25
    ended["total_cost_usd"] = 0.25
    feed(
        sdk.observer(),
        [
            (100, init()),
            (101, prompt()),
            (110, context_build()),
            (150, assistant("tool_use")),
            (151, paid),
            (160, tool_start("t1")),
            (170, tool_end("t1", False, 10)),
            (171, tool_results("t1")),
            (180, context_build()),
            # The provider reported no usage: no usage event follows.
            (200, assistant("end_turn")),
            (205, ended),
        ],
    )

    spans = sdk.spans()

    def window(name: str) -> list[tuple[int | None, int | None]]:
        return [(s.start_time, s.end_time) for s in all_named(spans, name)]

    assert window("invoke_agent") == [(at(100), at(205))]
    assert window("step") == [(at(101), at(171)), (at(171), at(205))]
    assert window("chat m") == [(at(110), at(150)), (at(180), at(200))]
    assert window("execute_tool Echo") == [(at(160), at(170))]
    chats = all_named(spans, "chat m")
    assert attr(chats[0], "gen_ai.usage.input_tokens") == 10
    assert attr(chats[1], "gen_ai.usage.input_tokens") is None
    assert attr(chats[0], "linch.cost_usd") == 0.25
    assert attr(chats[1], "linch.cost_usd") is None
    assert attr(one(spans, "invoke_agent"), "linch.cost_usd") == 0.25
    assert sum(p.count for p in sdk.points("gen_ai.client.token.usage")) == 2
    [duration] = sdk.points("gen_ai.client.operation.duration")
    assert duration.count == 2 and duration.sum == pytest.approx(0.060)
    assert all(error_type(s) is None for s in spans)


def test_input_tokens_include_the_cached_ones_as_the_convention_says() -> None:
    sdk = Sdk()
    usage = {
        "input_tokens": 100,
        "output_tokens": 5,
        "cache_read_tokens": 90_000,
        "cache_creation_tokens": 400,
    }
    ended = result("success")
    ended["total_usage"] = usage
    feed(
        sdk.observer(),
        [
            (100, init()),
            (101, prompt()),
            (110, context_build()),
            (150, assistant("end_turn")),
            (151, {"type": "usage", "usage": usage, "cumulative": usage}),
            (160, ended),
        ],
    )

    spans = sdk.spans()
    for span in (one(spans, "invoke_agent"), one(spans, "chat m")):
        assert attr(span, "gen_ai.usage.input_tokens") == 90_500
        assert attr(span, "gen_ai.usage.output_tokens") == 5
        assert attr(span, "gen_ai.usage.cache_read.input_tokens") == 90_000
        assert attr(span, "gen_ai.usage.cache_creation.input_tokens") == 400
    tokens = sdk.points("gen_ai.client.token.usage")
    [input] = [p for p in tokens if (p.attributes or {}).get("gen_ai.token.type") == "input"]
    assert (input.count, input.sum) == (1, 90_500)


def test_an_abort_during_a_call_marks_every_open_span_with_the_result_subtype() -> None:
    sdk = Sdk()
    feed(
        sdk.observer(),
        [(100, init()), (101, prompt()), (105, context_build()), (130, result("aborted"))],
    )

    spans = sdk.spans()
    chat = one(spans, "chat m")
    assert (chat.start_time, chat.end_time) == (at(105), at(130))
    for name in ("chat m", "step", "invoke_agent"):
        assert error_type(one(spans, name)) == "aborted"
    [duration] = sdk.points("gen_ai.client.operation.duration")
    assert (duration.attributes or {})["error.type"] == "aborted"
    assert duration.sum == pytest.approx(0.025)


def test_an_interrupted_tool_is_a_short_span_flagged_interrupted() -> None:
    sdk = Sdk()
    observer = sdk.observer()
    observer.observe(envelope(7, 500, tool_end("call_0_0", True, 0)))
    # A denied call: its start and end come together, and it is an error.
    observer.observe(envelope(8, 501, {**tool_start("call_0_1")}))
    observer.observe(envelope(9, 501, tool_end("call_0_1", True, 0)))
    observer.observe(envelope(10, 502, result("success")))

    spans = sdk.spans()
    tools = sorted(
        all_named(spans, "execute_tool Echo"), key=lambda s: attr(s, "gen_ai.tool.call.id")
    )
    assert len(tools) == 2
    assert (tools[0].start_time, tools[0].end_time) == (at(500), at(500))
    assert [error_type(t) for t in tools] == ["interrupted", "tool_error"]
    assert all(is_child(t, one(spans, "step")) for t in tools)
    run = one(spans, "invoke_agent")
    assert attr(run, "linch.resumed") is True and error_type(run) is None


def test_a_resume_without_presets_leaves_unknown_attributes_out() -> None:
    sdk = Sdk()
    observer = sdk.telemetry.observer(capture_content=False)
    assert observer is not None
    observer.observe(envelope(9, 200, context_build()))
    observer.observe(envelope(10, 210, result("success")))

    spans = sdk.spans()
    run = one(spans, "invoke_agent")
    assert attr(run, "linch.resumed") is True
    assert attr(run, "linch.run_id") == "r1"
    for key in ("gen_ai.conversation.id", "gen_ai.provider.name", "gen_ai.request.model"):
        assert attr(run, key) is None, key
    step = one(spans, "step")
    assert attr(step, "linch.turn_index") is None and step.start_time == at(200)
    assert one(spans, "chat").end_time == at(210), "a chat of an unknown model is just `chat`"


def test_events_in_an_order_no_run_produces_never_raise_and_every_span_ends() -> None:
    sdk = Sdk()
    observer = sdk.observer()
    compaction = {
        "type": "compaction",
        "messages_before": 5,
        "messages_after": 2,
        "tokens_before": 50,
        "tokens_after": 20,
        "strategy": "summary",
        "duration_ms": 3,
    }
    events = [
        tool_end("nobody", False, 9_000),
        assistant("end_turn"),
        usage_event(1, 1),
        compaction,
        init(),
        context_build(),
        context_build(),
        assistant("end_turn"),
        {"type": "error", "error": {}},
        usage_event(1, 1),
    ]
    for seq, event in enumerate(events):
        observer.observe(envelope(seq, 50 - seq, event))
    # Another run's envelope is not this run's.
    observer.observe(envelope(99, 60, result("success"), run_id="r2"))
    observer.observe(envelope(100, 70, result("error")))
    observer.observe(envelope(101, 80, init()))

    spans = sdk.spans()
    run = one(spans, "invoke_agent")
    assert run.end_time == at(70)
    assert error_type(run) == "run_error"
    assert all((s.end_time or 0) >= (s.start_time or 0) for s in spans)
    assert run.context is not None
    assert all(s.context is not None and s.context.trace_id == run.context.trace_id for s in spans)


def test_a_stream_that_fails_or_is_closed_ends_its_spans_now() -> None:
    sdk = Sdk()
    observer = sdk.observer()
    feed(observer, [(100, init()), (101, prompt()), (102, context_build())])
    observer.fail("RunError")
    observer.observe(envelope(4, 103, result("success")))

    spans = sdk.spans()
    assert names(spans) == ["chat m", "invoke_agent", "step"]
    for span in spans:
        assert error_type(span) == "RunError", span.name
        assert (span.end_time or 0) > at(103), f"{span.name} ends at the wall clock"

    sdk = Sdk()
    observer = sdk.observer()
    feed(observer, [(100, init()), (101, prompt()), (102, context_build())])
    observer.close()
    assert len(sdk.spans()) == 3
    assert all(error_type(s) is None for s in sdk.spans())


async def test_a_run_the_server_drops_ends_its_spans_with_the_error_type() -> None:
    sdk = Sdk()
    started = asyncio.Event()

    @tool(scope="read")
    async def stall() -> str:
        """Never answer."""
        started.set()
        await asyncio.sleep(60)
        return "never"

    async with Server() as server:
        agent = await server.open_agent(
            config([tool_turn("call_1", "stall", {})]), tools=[stall], telemetry=sdk.telemetry
        )
        run = await agent.run("s1", "hi")
        await started.wait()
        os.kill(server.pid, signal.SIGKILL)
        with pytest.raises(ConnectionClosed):
            await run.result()

    spans = sdk.spans()
    assert names(spans) == [f"chat {MODEL}", "execute_tool stall", "invoke_agent", "step"]
    for span in spans:
        if span.name == f"chat {MODEL}":
            assert error_type(span) is None, "the call had answered"
        else:
            assert error_type(span) == "ConnectionClosed", span.name


def test_without_an_sdk_provider_nothing_is_built() -> None:
    assert Telemetry().observer() is None, "the global providers are the API's stand-ins"
    sdk = Sdk()
    traced_only = Telemetry(tracer_provider=sdk.tracer_provider)
    assert traced_only.observer() is not None


def run_script(script: str) -> str:
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(SRC), str(SRC.parent / "tests")])}
    env.pop("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", None)
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, env=env, check=False
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


RUN_ONE = """
import asyncio
from scripted import config, text_turn
from linch_rust import Server

async def main():
    async with Server() as server:
        agent = await server.open_agent(config([text_turn("hello")]))
        print((await (await agent.run("s1", "hi")).result()).final_text)

asyncio.run(main())
"""


def test_runs_use_the_global_providers_by_default() -> None:
    script = (
        """
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

spans = InMemorySpanExporter()
tracer_provider = TracerProvider()
tracer_provider.add_span_processor(SimpleSpanProcessor(spans))
trace.set_tracer_provider(tracer_provider)
reader = InMemoryMetricReader()
metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))
"""
        + RUN_ONE
        + """
print(sorted(span.name for span in spans.get_finished_spans()))
data = reader.get_metrics_data()
print(sorted(m.name for r in data.resource_metrics for s in r.scope_metrics for m in s.metrics))
"""
    )
    assert run_script(script).splitlines() == [
        "hello",
        "['chat scripted-model', 'invoke_agent', 'step']",
        "['gen_ai.client.operation.duration', 'gen_ai.client.token.usage']",
    ]


def test_the_client_runs_without_the_otel_extra() -> None:
    script = (
        """
import sys
sys.modules["opentelemetry"] = None  # as if opentelemetry-api were not installed
import linch_rust
assert not any(name.startswith("linch_rust.otel") for name in sys.modules)
"""
        + RUN_ONE
    )
    assert run_script(script).splitlines() == ["hello"]


def test_the_provider_comes_from_init_when_the_host_names_none() -> None:
    sdk = Sdk()
    observer = sdk.telemetry.observer(capture_content=False)
    assert observer is not None
    feed(
        observer, [(100, init()), (101, prompt()), (102, context_build()), (103, result("success"))]
    )

    spans = sdk.spans()
    for name in ("invoke_agent", "step", "chat m"):
        assert attr(one(spans, name), "gen_ai.provider.name") == "scripted", name


def test_a_chat_names_the_model_that_served_it() -> None:
    sdk = Sdk()
    served = {**usage_event(1, 1), "actual_model": "served-2026"}
    feed(
        sdk.observer(),
        [(100, init()), (101, prompt()), (102, context_build()), (103, assistant("end_turn"))]
        + [(104, served), (105, result("success"))],
    )

    assert attr(one(sdk.spans(), "chat m"), "gen_ai.response.model") == "served-2026"


def test_a_compaction_span_starts_its_duration_before_its_event() -> None:
    sdk = Sdk()
    compaction = {
        "type": "compaction",
        "messages_before": 5,
        "messages_after": 2,
        "tokens_before": 50,
        "tokens_after": 20,
        "strategy": "summary",
        "duration_ms": 30,
    }
    feed(
        sdk.observer(),
        [(100, init()), (101, prompt()), (150, usage_event(7, 3)), (160, compaction)]
        + [(161, context_build()), (170, assistant("end_turn")), (171, result("success"))],
    )

    spans = sdk.spans()
    span = one(spans, "compaction")
    assert (span.start_time, span.end_time) == (at(130), at(160))
    [summary] = [chat for chat in all_named(spans, "chat m") if is_child(chat, span)]
    assert (summary.start_time, summary.end_time) == (at(130), at(150))
    assert attr(summary, "gen_ai.usage.input_tokens") == 7


def test_a_failed_tool_call_is_marked_with_its_error_code() -> None:
    sdk = Sdk()
    coded = {**tool_end("c1", True, 1), "error_code": "not_found"}
    feed(
        sdk.observer(),
        [(100, init()), (101, prompt()), (102, tool_start("c1")), (103, coded)]
        + [(104, tool_start("c2")), (105, tool_end("c2", True, 1)), (106, result("success"))],
    )

    tools = {attr(s, "gen_ai.tool.call.id"): s for s in all_named(sdk.spans(), "execute_tool Echo")}
    assert error_type(tools["c1"]) == "not_found"
    assert error_type(tools["c2"]) == "tool_error", "no code: as before"
