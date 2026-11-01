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


@pytest.mark.parametrize("kind", ["direct", "provider"])
async def test_late_service_registration_disposal_and_replacement(otel, kind):
    from types import SimpleNamespace

    from linch import Context
    from linch._telemetry import telemetry_for

    host, exporter, reader = otel
    context = Context()
    agent = SimpleNamespace(context=context)
    telemetry_for(agent)
    tracer_key, meter_key = (
        ("tracer", "meter") if kind == "direct" else ("tracer_provider", "meter_provider")
    )
    tracer = host.tracer_provider.get_tracer("test")
    meter = host.meter_provider.get_meter("test")
    tracer_service = tracer if kind == "direct" else host.tracer_provider
    meter_service = meter if kind == "direct" else host.meter_provider
    first_trace = context.register(tracer_key, tracer_service)
    first_meter = context.register(meter_key, meter_service)
    try:
        refreshed = telemetry_for(agent)
        with refreshed.tracer.start_as_current_span("late"):
            pass
        await refreshed.store_call("late", asyncio.sleep(0))
        assert [span.name for span in exporter.get_finished_spans()] == ["late"]
        assert reader.get_metrics_data() is not None
        assert telemetry_for(agent) is refreshed
        bound = refreshed.store_duration
        assert telemetry_for(agent).store_duration is bound
        await first_trace.dispose()
        await first_meter.dispose()
        refreshed = telemetry_for(agent)
        with refreshed.tracer.start_as_current_span("disposed"):
            pass
        assert [span.name for span in exporter.get_finished_spans()] == ["late"]
        assert not refreshed.metrics_enabled
        context.register(tracer_key, tracer_service)
        context.register(meter_key, meter_service)
        refreshed = telemetry_for(agent)
        with refreshed.tracer.start_as_current_span("replaced"):
            pass
        await refreshed.store_call("replaced", asyncio.sleep(0))
        assert [span.name for span in exporter.get_finished_spans()] == ["late", "replaced"]
    finally:
        await context.dispose()


async def test_parent_service_replacement_preserves_agent_isolation(otel):
    from types import SimpleNamespace

    from linch import Context
    from linch._telemetry import telemetry_for

    host, exporter, _ = otel
    child = host.scope("child", isolate_events=True)
    other = Context()
    child_agent, other_agent = SimpleNamespace(context=child), SimpleNamespace(context=other)
    original = telemetry_for(child_agent)
    telemetry_for(other_agent)
    override = child.register("tracer", trace.NoOpTracer())
    assert not telemetry_for(child_agent).tracing_enabled
    await override.dispose()
    with telemetry_for(child_agent).tracer.start_as_current_span("restored parent"):
        pass
    assert telemetry_for(child_agent) is original
    assert not telemetry_for(other_agent).tracing_enabled
    assert [span.name for span in exporter.get_finished_spans()] == ["restored parent"]
    await other.dispose()


@pytest.mark.parametrize("explicit_proxy", [False, True])
async def test_late_global_providers_refresh_deferred_bindings(otel, monkeypatch, explicit_proxy):
    from types import SimpleNamespace

    from opentelemetry import metrics
    from opentelemetry.metrics import _internal as metric_api

    from linch import Context
    from linch._telemetry import telemetry_for

    host, exporter, reader = otel
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", None)
    monkeypatch.setattr(metric_api, "_METER_PROVIDER", None)
    context = Context()
    if explicit_proxy:
        context.register("tracer", trace.ProxyTracer("test"))
        # Direct tracer takes precedence over a disabled scoped provider.
        context.register("tracer_provider", trace.NoOpTracerProvider())
    agent = SimpleNamespace(context=context)
    telemetry = telemetry_for(agent)
    assert not telemetry.tracing_enabled
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", host.tracer_provider)
    monkeypatch.setattr(metric_api, "_METER_PROVIDER", host.meter_provider)
    refreshed = telemetry_for(agent)
    assert refreshed.tracing_enabled
    with refreshed.tracer.start_as_current_span("late global"):
        pass
    await refreshed.store_call("late global", asyncio.sleep(0))
    assert [span.name for span in exporter.get_finished_spans()] == ["late global"]
    assert reader.get_metrics_data() is not None
    assert metrics.get_meter_provider() is host.meter_provider
    await context.dispose()


@pytest.mark.parametrize("direct", [False, True])
async def test_explicit_deferred_meter_activates_after_global_setup(otel, monkeypatch, direct):
    from types import SimpleNamespace

    from opentelemetry import metrics
    from opentelemetry.metrics import _internal as metric_api

    from linch import Context
    from linch._telemetry import telemetry_for

    host, _, reader = otel
    monkeypatch.setattr(metric_api, "_METER_PROVIDER", None)
    monkeypatch.setattr(
        metric_api, "_METER_PROVIDER_SET_ONCE", type(metric_api._METER_PROVIDER_SET_ONCE)()
    )
    monkeypatch.setattr(
        metric_api, "_PROXY_METER_PROVIDER", type(metric_api._PROXY_METER_PROVIDER)()
    )
    context = Context()
    if direct:
        context.register("meter", metrics.get_meter("host"))
    else:
        context.register("meter_provider", metrics.get_meter_provider())
    agent = SimpleNamespace(context=context)
    assert not telemetry_for(agent).metrics_enabled
    metrics.set_meter_provider(host.meter_provider)
    telemetry = telemetry_for(agent)
    assert telemetry.metrics_enabled
    await telemetry.store_call("late deferred meter", asyncio.sleep(0))
    assert reader.get_metrics_data() is not None
    await context.dispose()


@pytest.mark.parametrize("operation", ["chat", "store"])
async def test_inflight_operation_keeps_its_starting_metric_binding(otel, operation):
    from types import SimpleNamespace

    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    from linch import Context
    from linch._telemetry import ChatCall, telemetry_for
    from linch.types import ProviderRequest

    host, _, reader = otel
    context = Context()
    initial = context.register("meter_provider", host.meter_provider)
    agent = SimpleNamespace(
        context=context, model="test-model", provider=SimpleNamespace(id="test-provider")
    )
    session = SimpleNamespace(id="session", agent=agent)
    other_reader = InMemoryMetricReader()
    other_meter = MeterProvider(metric_readers=[other_reader])
    ready, release = asyncio.Event(), asyncio.Event()

    async def wait():
        ready.set()
        await release.wait()

    telemetry = telemetry_for(agent)
    task = None
    chat = None
    try:
        if operation == "chat":
            request = ProviderRequest(model=agent.model, messages=[], tools=[], system=[])
            chat = ChatCall(session, request)
        else:
            task = asyncio.create_task(telemetry.store_call("inflight", wait()))
            await ready.wait()
        await initial.dispose()
        context.register("meter_provider", other_meter)
        telemetry_for(agent)
        if chat is not None:
            chat.finish()
        else:
            release.set()
            await task
        assert reader.get_metrics_data() is not None
        assert other_reader.get_metrics_data() is None
    finally:
        release.set()
        if task is not None:
            await task
        if chat is not None:
            chat.finish()
        await context.dispose()
        other_meter.shutdown()
