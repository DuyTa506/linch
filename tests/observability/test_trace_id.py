from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from linch._telemetry import start_run_span
from linch._tracing import TraceIdFilter, trace_id_for
from linch.tools import ToolContext
from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn, ToolUseTurn


def _agent_for(context, provider, *, run_store=None, tools=None):
    return make_agent(
        provider,
        model="test-model",
        context=context,
        run_store=run_store,
        tools=tools,
        loop_guard=None,
    )


def test_trace_id_falls_back_to_stable_run_id_without_active_trace() -> None:
    assert trace_id_for("run-123") == "run-123"


def test_tool_context_exposes_read_only_trace_id() -> None:
    context = ToolContext(cwd=".", session_id="session", run_id="run-123", session_store=None)
    assert context.trace_id == "run-123"
    try:
        context.trace_id = "changed"  # type: ignore[misc]
    except AttributeError:
        pass
    else:
        raise AssertionError("trace_id must be read-only")


def test_boundary_event_trace_id_round_trips() -> None:
    from linch.events import ResultEvent, SystemEvent, event_from_dict, event_to_dict
    from linch.types import Usage

    system = SystemEvent(
        session_id="session",
        run_id="run-123",
        model="model",
        tools=[],
        permission_mode="default",
        cwd=".",
        trace_id="0" * 32,
    )
    result = ResultEvent(
        subtype="success",
        stop_reason="end_turn",
        total_usage=Usage(),
        duration_ms=1,
        trace_id="0" * 32,
    )
    assert event_from_dict(event_to_dict(system)).trace_id == "0" * 32
    assert event_from_dict(event_to_dict(result)).trace_id == "0" * 32


def test_trace_id_filter_preserves_explicit_record_value() -> None:
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "message", (), None)
    record.run_id = "run-123"
    record.trace_id = "explicit"
    assert TraceIdFilter().filter(record)
    assert record.trace_id == "explicit"


def test_trace_id_filter_uses_record_run_id() -> None:
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "message", (), None)
    record.run_id = "run-123"
    assert TraceIdFilter().filter(record)
    assert record.trace_id == "run-123"


def test_trace_id_filter_uses_configured_fallback() -> None:
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "message", (), None)
    assert TraceIdFilter("fallback").filter(record)
    assert record.trace_id == "fallback"


def test_resumed_run_keeps_opt_in_prompt_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    from linch import Context

    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "true")
    context = Context()
    provider = TracerProvider()
    context.register("tracer_provider", provider)
    agent = SimpleNamespace(
        context=context,
        provider=SimpleNamespace(id="test"),
        model="model",
    )
    session = SimpleNamespace(id="session", active_model=None, agent=agent)
    span = start_run_span(session, "checkpoint prompt", "run", resumed=True)
    try:
        assert span.is_recording()
        assert json.loads(span.attributes["gen_ai.input.messages"]) == "checkpoint prompt"
    finally:
        span.end()
        provider.shutdown()


@pytest.mark.parametrize("otel_enabled", [True, False])
@pytest.mark.asyncio
async def test_run_trace_propagates_to_boundary_events_store_tools_and_logs(
    otel_enabled: bool,
) -> None:
    from linch import Context, InMemoryRunStore, ToolRegistry, tool

    context = Context()
    exporter = InMemorySpanExporter() if otel_enabled else None
    provider = TracerProvider() if otel_enabled else trace.NoOpTracerProvider()
    if exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    context.register("tracer_provider", provider)
    store = InMemoryRunStore()
    logger = logging.getLogger("trace-id-test")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = Capture()
    handler.addFilter(TraceIdFilter())
    logger.addHandler(handler)
    seen_contexts: list[ToolContext] = []

    @tool
    def capture(ctx: ToolContext) -> str:
        """Capture the run-scoped tool context and emit one log record."""
        seen_contexts.append(ctx)
        logger.info("tool ran", extra={"run_id": ctx.run_id, "trace_id": ctx.trace_id})
        return "captured"

    tools = ToolRegistry()
    tools.register(capture)
    agent = _agent_for(
        context,
        ScriptedProvider([ToolUseTurn("capture", {}), TextTurn("done")]),
        run_store=store,
        tools=tools,
    )
    try:
        session = await agent.session()
        events = [event async for event in session.run("run")]
        system = next(event for event in events if event.type == "system")
        result = next(event for event in events if event.type == "result")
        record = await store.load_run(system.run_id)
        assert record is not None
        trace_id = system.trace_id
        assert trace_id is not None
        assert result.trace_id == trace_id == record.meta["trace_id"]
        assert seen_contexts[0].trace_id == trace_id
        assert records[0].trace_id == trace_id
        spans = (
            [span for span in exporter.get_finished_spans() if span.name == "invoke_agent"]
            if exporter is not None
            else []
        )
        assert len(spans) == int(otel_enabled)
        if spans:
            assert f"{spans[0].context.trace_id:032x}" == trace_id
    finally:
        logger.removeHandler(handler)
        await agent.close()
        if otel_enabled:
            provider.shutdown()


@pytest.mark.asyncio
async def test_iterator_created_before_host_span_uses_host_trace_id() -> None:
    from linch import Context, InMemoryRunStore

    context = Context()
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    context.register("tracer_provider", provider)
    store = InMemoryRunStore()
    agent = _agent_for(context, ScriptedProvider([TextTurn("done")]), run_store=store)
    try:
        session = await agent.session()
        iterator = session.run("run")
        with provider.get_tracer("host").start_as_current_span("host") as host:
            events = [event async for event in iterator]
        expected = f"{host.get_span_context().trace_id:032x}"
        assert next(event for event in events if event.type == "system").trace_id == expected
        assert next(event for event in events if event.type == "result").trace_id == expected
        system_run_id = next(event for event in events if event.type == "system").run_id
        record = await store.load_run(system_run_id)
        assert record is not None
        assert record.meta["trace_id"] == expected
    finally:
        await agent.close()
        provider.shutdown()


@pytest.mark.asyncio
async def test_run_span_closes_when_run_creation_fails() -> None:
    from linch import Context, InMemoryRunStore

    context = Context()
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    context.register("tracer_provider", provider)
    store = InMemoryRunStore()

    async def fail_create(*args: object, **kwargs: object) -> object:
        raise RuntimeError("create failed")

    store.create_run = fail_create  # type: ignore[method-assign]
    agent = _agent_for(context, ScriptedProvider([TextTurn("done")]), run_store=store)
    try:
        session = await agent.session()
        with pytest.raises(RuntimeError, match="create failed"):
            async for _event in session.run("run"):
                pass
        spans = [span for span in exporter.get_finished_spans() if span.name == "invoke_agent"]
        assert len(spans) == 1
    finally:
        await agent.close()
        provider.shutdown()
