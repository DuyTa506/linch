"""Live: OpenTelemetry spans and metrics of a real run, the order of its
event journal, token totals in the final event and a replay of the finished
run. Skips without the live environment.
"""

from __future__ import annotations

import pytest
from live_sched_support import kind, live_config, pg_dsn, seqs_increasing

from linch_rust import (
    InitEvent,
    ResultEvent,
    Server,
    ToolCallEndEvent,
    ToolCallStartEvent,
    UsageEvent,
    tool,
)

__all__ = ["kind", "pg_dsn"]

pytestmark = pytest.mark.live


async def test_a_real_run_has_spans_a_journal_in_order_totals_and_a_replay(
    kind: str, pg_dsn: str
) -> None:
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from linch_rust.otel import Telemetry

    exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    reader = InMemoryMetricReader()
    telemetry = Telemetry(
        tracer_provider=tracer_provider, meter_provider=MeterProvider(metric_readers=[reader])
    )

    @tool(scope="read")
    async def lookup(city: str) -> str:
        """Look up the population of a city."""
        return f"{city}: 2 million"

    config = live_config(kind, "observe", pg_dsn)
    async with Server() as server:
        agent = await server.open_agent(config, tools=[lookup], telemetry=telemetry)
        run = await agent.run(
            "s1",
            "Call the tool lookup with city 'Lyon', then say the population it returned.",
            run_id="observe-1",
        )
        streamed = [envelope async for envelope in run]
        replay = await agent.events("observe-1")
        tail = await agent.events("observe-1", after_seq=replay[2].seq)

    # The journal: order of the durable kinds, seqs, and a faithful replay.
    kinds = [e.event.type for e in replay]
    assert kinds[0] == "init" and kinds[-1] == "result", kinds
    assert seqs_increasing(replay)
    first_call = kinds.index("tool_call_start")
    assert kinds.index("assistant") < first_call < kinds.index("tool_call_end")
    assert kinds.index("tool_call_end") < len(kinds) - 1 - kinds[::-1].index("assistant")
    shown = {(e.seq, e.event.type) for e in streamed}
    assert {(e.seq, e.event.type) for e in replay} <= shown, "the replay is what the stream showed"
    assert tail == [e for e in replay if e.seq > replay[2].seq], "after_seq resumes the replay"

    # Totals: the final event carries the sum of the usage events.
    usage = [e.event for e in replay if isinstance(e.event, UsageEvent)]
    result = replay[-1].event
    assert isinstance(result, ResultEvent) and result.subtype == "success" and result.usage_complete
    assert len(usage) >= 2, "one call to ask for the tool, one to answer"
    for key in ("input_tokens", "output_tokens"):
        assert result.total_usage[key] == sum(u.usage[key] for u in usage) > 0
        assert usage[-1].cumulative[key] == result.total_usage[key]
    assert isinstance(replay[0].event, InitEvent) and replay[0].event.tools == ["lookup"]
    [(call, done)] = [
        (s.event, e.event)
        for s in replay
        for e in replay
        if isinstance(s.event, ToolCallStartEvent)
        and isinstance(e.event, ToolCallEndEvent)
        and s.event.tool_use_id == e.event.tool_use_id
    ]
    assert call.tool_name == done.tool_name == "lookup" and not done.is_error
    assert "Lyon" in (result.final_text or "") or "2" in (result.final_text or "")

    # Spans and metrics.
    spans = exporter.get_finished_spans()
    names = [span.name for span in spans]
    [root] = [span for span in spans if span.name == "invoke_agent"]
    assert any(name.startswith("chat ") for name in names)
    assert "execute_tool lookup" in names
    assert names.count("step") >= 2
    attributes = dict(root.attributes or {})
    assert attributes["gen_ai.operation.name"] == "invoke_agent"
    assert attributes["linch.run_id"] == "observe-1"
    assert attributes["gen_ai.conversation.id"] == "s1"
    # The span's input tokens include the cached ones (GenAI conventions).
    totals = result.total_usage
    cached = totals["cache_read_tokens"] + totals["cache_creation_tokens"]
    assert attributes["gen_ai.usage.input_tokens"] == totals["input_tokens"] + cached
    assert attributes["gen_ai.usage.output_tokens"] == result.total_usage["output_tokens"]
    trace_ids = {span.context.trace_id for span in spans if span.context}
    assert len(trace_ids) == 1, "one trace for the run"
    data = reader.get_metrics_data()
    assert data is not None
    metrics = {
        metric.name
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for metric in sm.metrics
    }
    assert {"gen_ai.client.operation.duration", "gen_ai.client.token.usage"} <= metrics
