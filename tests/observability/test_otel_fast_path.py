"""Fast-path contracts for no-op and non-recording OpenTelemetry spans."""

from __future__ import annotations

from types import SimpleNamespace

from opentelemetry import context as otel_context
from opentelemetry import trace


def _metric_points(reader, name):
    data = reader.get_metrics_data()
    return [
        point
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == name
        for point in metric.data.data_points
    ]


def _agent_for(context, provider):
    from linch import Agent, FeatureFlags, InMemorySessionStore

    return Agent(
        model="test-model",
        provider=provider,
        context=context,
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        loop_guard=None,
        include_partial_messages=True,
    )


async def test_no_sdk_stream_does_not_enter_use_span(monkeypatch):
    from linch import Context
    from linch.events import PartialAssistantEvent

    class TwoHundredDeltaProvider:
        id = "two-hundred-deltas"

        def context_window(self, model):
            return 128_000

        async def stream(self, request):
            from linch import Usage

            yield {"type": "message_start", "model": request.model}
            for _ in range(200):
                yield {"type": "text_delta", "text": "x"}
            yield {
                "type": "message_end",
                "stop_reason": "end_turn",
                "usage": Usage(),
            }

    def forbidden(*args, **kwargs):
        raise AssertionError("no-op telemetry must not enter trace.use_span")

    monkeypatch.setattr(trace, "use_span", forbidden)
    agent = _agent_for(
        Context(),
        TwoHundredDeltaProvider(),
    )
    try:
        session = await agent.session()
        events = [event async for event in session.run("stream")]
        partials = [event for event in events if isinstance(event, PartialAssistantEvent)]
        assert len(partials) == 200
        assert events[-1].subtype == "success"
    finally:
        await agent.close()


async def test_in_span_returns_raw_iterator_for_invalid_span():
    from linch._telemetry import in_span

    async def raw_events():
        yield "event"

    raw_iterator = raw_events()
    assert in_span(raw_iterator, trace.INVALID_SPAN) is raw_iterator
    assert [event async for event in raw_iterator] == ["event"]


async def test_trace_run_returns_exact_raw_iterator_for_noop_tracer():
    from linch import Context
    from linch._telemetry import trace_run

    context = Context()
    context.register("tracer", trace.NoOpTracer())
    agent = SimpleNamespace(
        context=context,
        model="test-model",
        provider=SimpleNamespace(id="test-provider"),
    )
    session = SimpleNamespace(id="session", agent=agent)

    async def raw_events():
        yield "event"

    raw_iterator = raw_events()

    def factory(current_session, prompt, opts, **kwargs):
        return raw_iterator

    decorated = trace_run(factory)
    result = decorated(session, "prompt", None, run_id="run", resume_checkpoint=None)
    assert result is raw_iterator
    assert [event async for event in result] == ["event"]
    await context.dispose()


async def test_metrics_survive_noop_tracer_without_use_span(monkeypatch):
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    from linch import Context, Usage
    from linch.evals import ScriptedProvider, TextTurn

    def forbidden(*args, **kwargs):
        raise AssertionError("non-recording telemetry must not enter trace.use_span")

    monkeypatch.setattr(trace, "use_span", forbidden)
    context = Context()
    reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[reader])
    context.register("tracer_provider", trace.NoOpTracerProvider())
    context.register("meter_provider", meter_provider)
    agent = _agent_for(
        context,
        ScriptedProvider([TextTurn("answer", usage=Usage(7, 4))]),
    )
    try:
        session = await agent.session()
        events = [event async for event in session.run("metrics")]
        assert events[-1].subtype == "success"

        duration_points = _metric_points(reader, "gen_ai.client.operation.duration")
        assert len(duration_points) == 1
        assert duration_points[0].count == 1
        token_points = _metric_points(reader, "gen_ai.client.token.usage")
        assert {point.attributes["gen_ai.token.type"]: point.sum for point in token_points} == {
            "input": 7,
            "output": 4,
        }
    finally:
        await agent.close()
        meter_provider.shutdown()


async def test_always_off_tracer_preserves_external_caller_context():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.sdk.trace.sampling import ALWAYS_OFF

    from linch import Context, Usage

    class ContextProbeProvider:
        id = "probe"

        def __init__(self):
            self.seen = []

        def context_window(self, model):
            return 128_000

        async def stream(self, request):
            for item in (
                {"type": "message_start", "model": request.model},
                {"type": "text_delta", "text": "answer"},
                {
                    "type": "message_end",
                    "stop_reason": "end_turn",
                    "usage": Usage(1, 1),
                },
            ):
                self.seen.append(trace.get_current_span().get_span_context())
                yield item

    exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider(sampler=ALWAYS_OFF)
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    context = Context()
    context.register("tracer_provider", tracer_provider)
    provider = ContextProbeProvider()
    agent = _agent_for(context, provider)
    caller_context = trace.SpanContext(
        trace_id=0x4BF92F3577B34DA6A3CE929D0E0E4736,
        span_id=0x00F067AA0BA902B7,
        trace_flags=trace.TraceFlags(1),
        is_remote=True,
    )
    caller = trace.NonRecordingSpan(caller_context)
    token = otel_context.attach(trace.set_span_in_context(caller))
    try:
        session = await agent.session()
        events = []
        async for event in session.run("caller context"):
            assert trace.get_current_span() is caller
            events.append(event)
        assert events[-1].subtype == "success"
    finally:
        otel_context.detach(token)
        await agent.close()
        tracer_provider.shutdown()

    assert provider.seen
    assert all(span_context.is_valid for span_context in provider.seen)
    assert {span_context.trace_id for span_context in provider.seen} == {caller_context.trace_id}
    assert not exporter.get_finished_spans()
