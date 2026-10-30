"""Private telemetry helpers use injected OTel services without global state."""

import asyncio

import pytest
from opentelemetry import trace


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
