"""Private telemetry helpers use injected OTel services without global state."""

import asyncio

import pytest
from opentelemetry import trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


@pytest.fixture
def otel():
    from linch import Context

    context = Context()
    exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[reader])
    context.register("tracer_provider", tracer_provider)
    context.register("meter_provider", meter_provider)
    yield context, exporter, reader
    tracer_provider.shutdown()
    meter_provider.shutdown()


async def test_iterator_restores_caller_context_and_closes_on_aclose(otel):
    from linch._telemetry import Telemetry, in_span

    context, exporter, _ = otel
    telemetry = Telemetry(context)
    with telemetry.tracer.start_as_current_span("host") as host:
        span = telemetry.tracer.start_span("invoke_agent")

        async def events():
            assert trace.get_current_span() is span
            yield 1
            assert trace.get_current_span() is span
            yield 2

        iterator = in_span(events(), span)
        assert await anext(iterator) == 1
        assert trace.get_current_span() is host
        await iterator.aclose()
        assert trace.get_current_span() is host
    assert [s.name for s in exporter.get_finished_spans()] == ["invoke_agent", "host"]


async def test_store_failure_records_duration_and_error_without_content(otel):
    from linch._telemetry import Telemetry

    context, _, reader = otel
    telemetry = Telemetry(context)

    async def failing():
        await asyncio.sleep(0)
        raise ValueError("private database payload")

    with pytest.raises(ValueError):
        await telemetry.store_call("run.save_checkpoint", failing())
    metrics = reader.get_metrics_data().resource_metrics[0].scope_metrics[0].metrics
    metric = next(m for m in metrics if m.name == "linch.store.duration")
    point = metric.data.data_points[0]
    assert point.count == 1
    assert point.sum > 0
    assert point.attributes == {"op": "run.save_checkpoint", "error.type": "ValueError"}


async def test_provider_services_are_isolated_and_errors_are_private(otel):
    from linch._telemetry import Telemetry, set_error

    context, exporter, _ = otel
    telemetry = Telemetry(context)
    with telemetry.tracer.start_as_current_span("chat") as span:
        set_error(span, ValueError("private prompt"))
    recorded = exporter.get_finished_spans()[0]
    assert recorded.attributes["error.type"] == "ValueError"
    assert recorded.status.status_code is trace.StatusCode.ERROR
    assert recorded.status.description is None
    assert not recorded.events
    assert "private prompt" not in str(recorded.attributes)
