"""Direct source instrumentation: exported trees and measurements are the contract."""

from __future__ import annotations

import asyncio
import json

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn
from tests.support.tools import tool_registry


def agent_for(context, turns=None, **kwargs):
    provider = kwargs.pop("provider", None)
    if provider is None:
        provider = ScriptedProvider(turns or [TextTurn("private pong")])
        provider.id = "scripted"
    return make_agent(
        provider,
        model="test-model",
        context=context,
        run_store=kwargs.pop("run_store", None),
        loop_guard=None,
        **kwargs,
    )


async def collect(session, prompt="private prompt"):
    return [event async for event in session.run(prompt)]


def operation_spans(exporter, operation):
    return [
        s
        for s in exporter.get_finished_spans()
        if s.attributes.get("gen_ai.operation.name") == operation
    ]


def metric_points(reader, name):
    data = reader.get_metrics_data()
    return [
        p
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == name
        for p in metric.data.data_points
    ]


async def test_text_run_tree_usage_metrics_and_caller_context(otel):
    from linch import Usage
    from tests.support.providers import TextTurn

    context, exporter, reader = otel
    agent = agent_for(context, [TextTurn("private pong", usage=Usage(11, 5, 3, 2))])
    session = await agent.session()
    with context.tracer_provider.get_tracer("host").start_as_current_span("host") as host:
        events = []
        async for event in session.run("private prompt"):
            assert trace.get_current_span() is host
            events.append(event)
    assert events[-1].subtype == "success"
    assert all(
        s.status.status_code is trace.StatusCode.UNSET for s in exporter.get_finished_spans()
    )
    (run,) = operation_spans(exporter, "invoke_agent")
    (step,) = operation_spans(exporter, "step")
    (chat,) = operation_spans(exporter, "chat")
    assert run.parent.span_id == host.get_span_context().span_id
    assert step.parent.span_id == run.context.span_id
    assert chat.parent.span_id == step.context.span_id
    assert {run.context.trace_id, step.context.trace_id, chat.context.trace_id} == {
        host.get_span_context().trace_id
    }
    assert chat.attributes["gen_ai.provider.name"] == "scripted"
    assert chat.attributes["gen_ai.request.model"] == "test-model"
    assert chat.attributes["gen_ai.response.finish_reasons"] == ("end_turn",)
    assert chat.attributes["gen_ai.usage.input_tokens"] == 11
    assert chat.attributes["gen_ai.usage.output_tokens"] == 5
    assert chat.attributes["gen_ai.usage.cache_read.input_tokens"] == 3
    assert chat.attributes["gen_ai.usage.cache_creation.input_tokens"] == 2
    assert run.attributes["gen_ai.conversation.id"] == session.id
    assert run.attributes["linch.run_id"] == next(e.run_id for e in events if e.type == "system")
    assert step.attributes["linch.turn_index"] == 0
    points = metric_points(reader, "gen_ai.client.token.usage")
    assert {(p.attributes["gen_ai.token.type"], p.sum, p.count) for p in points} == {
        ("input", 11, 1),
        ("output", 5, 1),
    }
    assert all(
        "error.type" not in p.attributes
        for p in metric_points(reader, "gen_ai.client.operation.duration")
    )
    assert all("error.type" not in p.attributes for p in points)
    assert all(
        p.sum > 0 and p.count == 1
        for p in metric_points(reader, "gen_ai.client.operation.duration")
    )
    await agent.close()


@pytest.mark.parametrize("capture", [False, True])
async def test_tool_tree_and_content_switch(otel, monkeypatch, capture):
    from linch import tool
    from tests.support.providers import TextTurn, ToolUseTurn

    if capture:
        monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "true")
    else:
        monkeypatch.delenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", raising=False)
    context, exporter, _ = otel

    @tool
    def echo(secret: str) -> str:
        """Echo the input."""
        return "private result " + secret

    tools = tool_registry(echo)
    agent = agent_for(
        context,
        [ToolUseTurn("echo", {"secret": "private argument"}), TextTurn("private pong")],
        tools=tools,
    )
    await collect(await agent.session())
    (run,) = operation_spans(exporter, "invoke_agent")
    steps = operation_spans(exporter, "step")
    chats = operation_spans(exporter, "chat")
    (tool_span,) = operation_spans(exporter, "execute_tool")
    assert len(steps) == len(chats) == 2
    assert all(s.parent.span_id == run.context.span_id for s in steps)
    assert tool_span.parent.span_id == steps[0].context.span_id
    assert tool_span.attributes["gen_ai.tool.name"] == "echo"
    assert tool_span.attributes["gen_ai.tool.call.id"] == "scripted_tool_1"
    all_data = str(
        [(s.attributes, s.events, s.status.description) for s in exporter.get_finished_spans()]
    )
    for content in ("private prompt", "private argument", "private result", "private pong"):
        assert (content in all_data) is capture
    if capture:
        assert json.loads(chats[0].attributes["gen_ai.input.messages"])[0]["role"] == "user"
        assert json.loads(tool_span.attributes["gen_ai.tool.call.arguments"]) == {
            "secret": "private argument"
        }
    await agent.close()


@pytest.mark.parametrize("failure", ["raise", "result"])
async def test_failing_tool_sets_error_status(otel, failure):
    from linch import ToolResult, tool
    from tests.support.providers import TextTurn, ToolUseTurn

    context, exporter, _ = otel

    @tool
    def fail() -> ToolResult:
        """Fail deliberately."""
        if failure == "raise":
            raise ValueError("private tool error")
        return ToolResult(content="private tool error", is_error=True)

    tools = tool_registry(fail)
    agent = agent_for(context, [ToolUseTurn("fail", {}), TextTurn("recovered")], tools=tools)
    events = await collect(await agent.session())
    assert events[-1].subtype == "success"
    (span,) = operation_spans(exporter, "execute_tool")
    assert span.status.status_code is trace.StatusCode.ERROR
    assert span.attributes["error.type"] == ("ValueError" if failure == "raise" else "tool_error")
    assert "private tool error" not in str((span.attributes, span.events, span.status.description))
    await agent.close()


async def test_provider_error_sets_chat_step_and_run_status(otel):
    from linch import ProviderError

    context, exporter, reader = otel

    class BrokenProvider:
        id = "broken"

        def context_window(self, model):
            return 128_000

        async def stream(self, req):
            yield {"type": "message_start", "model": req.model}
            raise ProviderError("private provider error", retryable=False)

    agent = agent_for(context, provider=BrokenProvider())
    events = await collect(await agent.session())
    assert events[-1].subtype == "error"
    for op in ("chat", "step", "invoke_agent"):
        (span,) = operation_spans(exporter, op)
        assert span.status.status_code is trace.StatusCode.ERROR
        assert span.attributes["error.type"] == "ProviderError"
        assert "private provider error" not in str(
            (span.attributes, span.events, span.status.description)
        )
    points = metric_points(reader, "gen_ai.client.operation.duration")
    assert points[0].attributes["error.type"] == "ProviderError"
    assert not metric_points(reader, "gen_ai.client.token.usage")
    await agent.close()


async def test_agents_with_different_injected_providers_are_isolated(otel):
    from linch import Context

    context, exporter, _ = otel
    other_context = Context()
    other_exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(other_exporter))
    other_context.register("tracer_provider", provider)
    first, second = agent_for(context), agent_for(other_context)
    a, b = await first.session(), await second.session()
    await asyncio.gather(collect(a), collect(b))
    assert {s.attributes["gen_ai.conversation.id"] for s in exporter.get_finished_spans()} == {a.id}
    assert {
        s.attributes["gen_ai.conversation.id"] for s in other_exporter.get_finished_spans()
    } == {b.id}
    await first.close()
    await second.close()
    provider.shutdown()


async def test_resumed_run_keeps_run_identity_and_current_parent(otel):
    from linch import InMemoryRunStore, RunCheckpoint, RunOptions, Usage
    from linch.loop.contracts import _build_run_contract
    from linch.run_store import run_meta_with_contract

    context, exporter, reader = otel
    store = InMemoryRunStore()
    agent = agent_for(context, run_store=store)
    session = await agent.session()
    record = await store.create_run(
        session.id,
        meta=run_meta_with_contract(_build_run_contract(session, "resume", RunOptions())),
    )
    await store.save_checkpoint(
        record.id,
        RunCheckpoint(phase="turn_complete", prompt="resume", turn_index=0, total_usage=Usage()),
    )
    with context.tracer_provider.get_tracer("host").start_as_current_span("resume host") as host:
        events = [e async for e in session.resume(record.id)]
    assert events[-1].subtype == "success"
    (span,) = operation_spans(exporter, "invoke_agent")
    assert span.parent.span_id == host.get_span_context().span_id
    assert span.attributes["linch.run_id"] == record.id
    assert span.attributes["linch.resumed"] is True
    (step,) = operation_spans(exporter, "step")
    assert step.attributes["linch.turn_index"] == 1
    ops = {p.attributes["op"] for p in metric_points(reader, "linch.store.duration")}
    assert {
        "run.load_run",
        "run.load_events",
        "run.save_checkpoint_with_events",
        "session.append",
    } <= ops
    assert len(exporter.get_finished_spans()) == 4  # store calls are metrics only
    await agent.close()


async def test_real_subagent_is_nested_under_parent_tool_and_run(otel):
    from linch import tool
    from linch.subagents.default_agent import DEFAULT_AGENT
    from linch.subagents.runner import RunSubagentArgs, run_subagent
    from tests.support.providers import TextTurn, ToolUseTurn

    context, exporter, _ = otel
    parent = None
    agent = None

    @tool
    async def delegate() -> str:
        """Run a child agent."""
        result = await run_subagent(
            RunSubagentArgs(
                parent_session=parent,
                parent_agent=agent,
                definition=DEFAULT_AGENT,
                prompt="child task",
                display_name="child",
                subagent_run_id="child",
            )
        )
        return result.final_text

    tools = tool_registry(delegate)
    agent = agent_for(
        context,
        [ToolUseTurn("delegate", {}), TextTurn("child done"), TextTurn("parent done")],
        tools=tools,
    )
    parent = await agent.session()
    await collect(parent)
    runs = operation_spans(exporter, "invoke_agent")
    assert len(runs) == 2
    child_run = next(s for s in runs if s.attributes["gen_ai.conversation.id"] != parent.id)
    parent_run = next(s for s in runs if s.attributes["gen_ai.conversation.id"] == parent.id)
    (tool_span,) = operation_spans(exporter, "execute_tool")
    assert child_run.parent.span_id == tool_span.context.span_id
    assert child_run.context.trace_id == parent_run.context.trace_id
    await agent.close()


async def test_compaction_has_span_and_accounted_chat_child(otel):
    from linch import DefaultCompaction, Message, TextBlock
    from tests.support.providers import TextTurn

    context, exporter, _ = otel
    agent = agent_for(
        context,
        [TextTurn("short summary"), TextTurn("answer")],
        compaction=DefaultCompaction(),
    )
    agent.provider.context_window = lambda model: 1000
    session = await agent.session()
    for _ in range(12):
        await session.append(
            [
                Message(role="user", content=[TextBlock(text="long context " * 50)]),
                Message(role="assistant", content=[TextBlock(text="history " * 50)]),
            ]
        )
    events = await collect(session)
    assert events[-1].subtype == "success"
    (compact,) = operation_spans(exporter, "compaction")
    (step,) = operation_spans(exporter, "step")
    assert compact.parent.span_id == step.context.span_id
    chats = operation_spans(exporter, "chat")
    assert len(chats) == 2
    assert sum(s.parent.span_id == compact.context.span_id for s in chats) == 1
    await agent.close()


async def test_subagent_definition_provider_call_uses_agent_context(otel):
    from linch import generate_subagent_definition
    from tests.support.providers import TextTurn

    context, exporter, reader = otel
    definition = {
        "name": "research-helper",
        "description": "Research a topic",
        "body": "You research.",
    }
    agent = agent_for(context, [TextTurn(json.dumps(definition))])
    generated = await generate_subagent_definition(agent, "research", existing_names=[])
    assert generated.name == "research-helper"
    (chat,) = operation_spans(exporter, "chat")
    assert chat.attributes["gen_ai.request.model"] == "test-model"
    assert metric_points(reader, "gen_ai.client.operation.duration")[0].count == 1
    await agent.close()


async def test_standalone_compaction_chat_is_injected_and_nested(otel):
    from linch import DefaultCompaction, Message, TextBlock
    from linch.abort import AbortContext
    from linch.compaction import run_forced_compaction
    from tests.support.providers import TextTurn

    context, exporter, reader = otel
    agent = agent_for(context, [TextTurn("summary")], compaction=DefaultCompaction())
    session = await agent.session()
    for _ in range(12):
        await session.append(
            [
                Message("user", [TextBlock("old prompt")]),
                Message("assistant", [TextBlock("old response")]),
            ]
        )
    await run_forced_compaction(session, agent, AbortContext())
    (compact,) = operation_spans(exporter, "compaction")
    (chat,) = operation_spans(exporter, "chat")
    assert chat.parent.span_id == compact.context.span_id
    assert metric_points(reader, "gen_ai.client.operation.duration")[0].count == 1
    await agent.close()


async def test_run_close_failure_restores_state_and_ends_step(otel):
    from types import SimpleNamespace

    from linch._telemetry import start_step, trace_run

    context, exporter, _ = otel
    agent = agent_for(context)
    session = await agent.session()

    @trace_run
    async def broken(session, prompt, opts, **kwargs):
        start_step(session, 0)
        try:
            yield SimpleNamespace(type="notice")
        finally:
            raise RuntimeError("close failed")

    iterator = broken(session, "go", None, run_id="test-run", resume_checkpoint=None)
    await anext(iterator)
    with pytest.raises(RuntimeError, match="close failed"):
        await iterator.aclose()
    assert session._telemetry_run is None
    assert len(operation_spans(exporter, "step")) == 1
    assert len(operation_spans(exporter, "invoke_agent")) == 1
    assert operation_spans(exporter, "invoke_agent")[0].status.status_code is trace.StatusCode.ERROR
    await agent.close()


async def test_durable_inbox_store_call_is_measured(otel):
    from linch import Message, TextBlock
    from linch.durability import DurabilityOptions

    context, _, reader = otel
    agent = agent_for(context, durability=DurabilityOptions(durable_inbox=True))
    session = await agent.session()
    await session.notify(Message("user", [TextBlock("notification")]), delivery_id="delivery-1")
    await collect(session)
    points = metric_points(reader, "linch.store.duration")
    assert any(p.attributes["op"] == "session.commit_inbox" for p in points)
    await agent.close()


@pytest.mark.parametrize("resume", [False, True])
async def test_public_iterator_close_finishes_before_session_becomes_idle(otel, resume):
    from linch import InMemoryRunStore, RunCheckpoint, RunOptions, Usage
    from linch.loop.contracts import _build_run_contract
    from linch.run_store import run_meta_with_contract

    context, exporter, _ = otel
    store = InMemoryRunStore()
    agent = agent_for(context, run_store=store)
    session = await agent.session()
    if resume:
        record = await store.create_run(
            session.id,
            meta=run_meta_with_contract(_build_run_contract(session, "resume", RunOptions())),
        )
        await store.save_checkpoint(
            record.id,
            RunCheckpoint(
                phase="turn_complete", prompt="resume", turn_index=0, total_usage=Usage()
            ),
        )
        iterator = session.resume(record.id)
    else:
        iterator = session.run("go")
    await anext(iterator)
    await iterator.aclose()
    assert session._idle.is_set()
    assert session._telemetry_run is None
    assert len(operation_spans(exporter, "invoke_agent")) == 1
    await agent.close()


async def test_child_context_overrides_ancestor_cached_instruments(otel):
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    from linch import Usage
    from tests.support.providers import TextTurn

    context, exporter, reader = otel
    parent = agent_for(context, [TextTurn("parent", usage=Usage(1, 1))])
    parent_session = await parent.session()
    await collect(parent_session)
    child_exporter = InMemorySpanExporter()
    child_provider = TracerProvider()
    child_provider.add_span_processor(SimpleSpanProcessor(child_exporter))
    child_reader = InMemoryMetricReader()
    child_meter = MeterProvider(metric_readers=[child_reader])
    child_context = parent.context.scope("child tenant")
    child_context.register("tracer_provider", child_provider)
    child_context.register("meter_provider", child_meter)
    child = agent_for(child_context, [TextTurn("child", usage=Usage(2, 2))])
    child_session = await child.session()
    try:
        await collect(child_session)
        assert {
            s.attributes["gen_ai.conversation.id"] for s in child_exporter.get_finished_spans()
        } == {child_session.id}
        assert {s.attributes["gen_ai.conversation.id"] for s in exporter.get_finished_spans()} == {
            parent_session.id
        }
        assert {p.sum for p in metric_points(reader, "gen_ai.client.token.usage")} == {1}
        assert {p.sum for p in metric_points(child_reader, "gen_ai.client.token.usage")} == {2}
    finally:
        await child.close()
        await parent.close()
        child_provider.shutdown()
        child_meter.shutdown()


async def test_failed_primary_parse_keeps_known_usage_and_actual_model(otel):
    from linch import Usage

    context, exporter, reader = otel

    class InvalidStopProvider:
        id = "fake"

        def context_window(self, model):
            return 128_000

        async def stream(self, req):
            yield {"type": "message_start", "model": "actual-model"}
            yield {"type": "message_end", "stop_reason": "invalid", "usage": Usage(7, 4)}

    agent = agent_for(context, provider=InvalidStopProvider(), max_retries=0)
    events = await collect(await agent.session())
    assert events[-1].total_usage == Usage(7, 4)
    (chat,) = operation_spans(exporter, "chat")
    assert chat.attributes["gen_ai.usage.input_tokens"] == 7
    assert chat.attributes["gen_ai.usage.output_tokens"] == 4
    assert chat.attributes["gen_ai.response.model"] == "actual-model"
    assert {
        (p.attributes["gen_ai.token.type"], p.sum)
        for p in metric_points(reader, "gen_ai.client.token.usage")
    } == {("input", 7), ("output", 4)}
    await agent.close()


@pytest.mark.parametrize("tracing", [False, True])
async def test_custom_standalone_compaction_records_physical_call(otel, tracing):
    from linch import Message, TextBlock, Usage
    from linch.abort import AbortContext
    from linch.compaction import run_forced_compaction
    from linch.types import ProviderRequest
    from tests.support.providers import TextTurn

    class DirectStreamCompaction:
        id = "direct-stream"

        async def compact(self, ctx, provider):
            request = ProviderRequest(model=ctx.model, messages=ctx.messages, tools=[], system=[])
            text = ""
            async for event in provider.stream(request):
                if event["type"] == "text_delta":
                    text += event["text"]
            return [Message("user", [TextBlock(text)])]

    context, exporter, reader = otel
    if not tracing:
        context.register("tracer", trace.NoOpTracer())
    agent = agent_for(
        context, [TextTurn("summary", usage=Usage(7, 4))], compaction=DirectStreamCompaction()
    )
    session = await agent.session()
    await session.append([Message("user", [TextBlock("long history")])])
    try:
        await run_forced_compaction(session, agent, AbortContext())
        if tracing:
            (compact,) = operation_spans(exporter, "compaction")
            (chat,) = operation_spans(exporter, "chat")
            assert chat.parent.span_id == compact.context.span_id
        else:
            assert not exporter.get_finished_spans()
        assert metric_points(reader, "gen_ai.client.operation.duration")[0].count == 1
        assert {
            (p.attributes["gen_ai.token.type"], p.sum)
            for p in metric_points(reader, "gen_ai.client.token.usage")
        } == {("input", 7), ("output", 4)}
    finally:
        await agent.close()


async def test_recovered_provider_attempt_does_not_fail_step_or_run(otel):
    from linch import ProviderError, Usage

    class RetryOnceProvider:
        id = "retry-once"

        def __init__(self):
            self.calls = 0

        def context_window(self, model):
            return 128_000

        async def stream(self, request):
            self.calls += 1
            yield {"type": "message_start", "model": request.model}
            if self.calls == 1:
                raise ProviderError("retry me", retryable=True)
            yield {"type": "text_delta", "text": "recovered"}
            yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}

    context, exporter, _ = otel
    provider = RetryOnceProvider()
    agent = agent_for(context, provider=provider, max_retries=1)
    try:
        events = await collect(await agent.session())
        assert events[-1].subtype == "success"
        assert provider.calls == 2
        chats = operation_spans(exporter, "chat")
        assert [span.status.status_code for span in chats] == [
            trace.StatusCode.ERROR,
            trace.StatusCode.UNSET,
        ]
        for operation in ("step", "invoke_agent"):
            (span,) = operation_spans(exporter, operation)
            assert span.status.status_code is trace.StatusCode.UNSET
            assert "error.type" not in span.attributes
    finally:
        await agent.close()


async def test_failure_after_closed_step_only_fails_run(otel):
    from types import SimpleNamespace

    from linch._telemetry import end_step, start_step, trace_run

    context, exporter, _ = otel
    agent = agent_for(context)
    session = await agent.session()

    @trace_run
    async def run(session, prompt, opts, **kwargs):
        start_step(session, 0)
        end_step(session)
        yield SimpleNamespace(type="notice")
        raise RuntimeError("between turns")

    try:
        with pytest.raises(RuntimeError, match="between turns"):
            async for _ in run(session, "go", None, run_id="run", resume_checkpoint=None):
                pass
        (step,) = operation_spans(exporter, "step")
        (run_span,) = operation_spans(exporter, "invoke_agent")
        assert step.status.status_code is trace.StatusCode.UNSET
        assert run_span.status.status_code is trace.StatusCode.ERROR
    finally:
        await agent.close()
