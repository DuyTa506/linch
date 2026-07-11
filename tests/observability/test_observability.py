"""Tests for the Phase-10 observability subsystem.

Unit tests cover the dispatcher (sync/async/isolation) and normalize_observers.
Integration tests use a RecordingProvider-style fake to drive run_loop and assert
that SpanCollector captures the expected span tree.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Dispatcher unit tests (no loop / no API)
# ---------------------------------------------------------------------------


def _make_text_provider():
    """Minimal fake provider that returns a text response (no tool calls)."""
    from linch.types import Usage

    class TextProvider:
        id = "fake-text"

        def context_window(self, model):
            return 128_000

        async def stream(self, req):
            yield {"type": "message_start", "model": req.model}
            yield {"type": "text_delta", "text": "pong"}
            yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}

    return TextProvider()


def _make_tool_provider(tool_name: str = "Echo", tool_input: dict | None = None):
    """Provider that returns one tool call then a text response on the next call."""
    import json

    from linch.types import Usage

    class ToolThenTextProvider:
        id = "fake-tool"
        call_count = 0

        def context_window(self, model):
            return 128_000

        async def stream(self, req):
            self.call_count += 1
            yield {"type": "message_start", "model": req.model}
            if self.call_count == 1:
                tid = "t1"
                yield {"type": "tool_use_start", "id": tid, "name": tool_name}
                yield {
                    "type": "tool_use_input_delta",
                    "id": tid,
                    "json_delta": json.dumps(tool_input or {}),
                }
                yield {"type": "tool_use_end", "id": tid}
                yield {
                    "type": "message_end",
                    "stop_reason": "tool_use",
                    "usage": Usage(),
                }
            else:
                yield {"type": "text_delta", "text": "done"}
                yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}

    return ToolThenTextProvider()


def _make_error_provider():
    """Provider that raises a generic exception during streaming."""
    from linch.errors import ProviderError

    class ErrorProvider:
        id = "fake-error"

        def context_window(self, model):
            return 128_000

        async def stream(self, req):
            yield {"type": "message_start", "model": req.model}
            raise ProviderError("simulated provider failure")
            yield  # noqa: F401 — makes the function an async generator

    return ErrorProvider()


def _make_agent(
    provider,
    *,
    tool_name="Echo",
    hooks=None,
    tool_result=None,
):
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.sessions import InMemorySessionStore
    from linch.tools import ToolResult
    from linch.tools.registry import empty_tools

    class _DummyTool:
        description = "Echo tool"
        input_schema = {"type": "object", "properties": {}}
        scope = "read"
        parallel = True
        tags: tuple = ()

        def __init__(self, name: str) -> None:
            self.name = name

        def validate(self, raw):
            return raw

        def summarize(self, inp):
            return self.name

        def resources(self, inp):
            return []

        async def execute(self, inp, ctx):
            return tool_result or ToolResult(content="echo-ok")

    return Agent(
        model="test-model",
        provider=provider,
        tools=empty_tools(_DummyTool(tool_name)),
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        loop_guard=None,
        hooks=hooks,
    )


def _observe(observers):
    from linch.hooks import RunTelemetryHook

    return [RunTelemetryHook(observers)]


async def _collect(session, prompt="go"):
    events = []
    async for event in session.run(prompt):
        events.append(event)
    return events


# ---------------------------------------------------------------------------
# Dispatcher units
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatcher_sync_observer():
    from linch.observability import ObserverDispatcher, RunInfo

    called_with = []

    class Obs:
        def on_run_start(self, info):
            called_with.append(info)

    hub = ObserverDispatcher([Obs()])
    info = RunInfo(run_id="r1", session_id="s1", model="m", prompt="p")
    await hub.dispatch("on_run_start", info)
    assert called_with == [info]


@pytest.mark.asyncio
async def test_dispatcher_async_observer():
    from linch.observability import ObserverDispatcher, TurnInfo

    result = []

    class AsyncObs:
        async def on_turn_start(self, info):
            await asyncio.sleep(0)
            result.append(info.turn_index)

    hub = ObserverDispatcher([AsyncObs()])
    await hub.dispatch("on_turn_start", TurnInfo(run_id="r", turn_index=3))
    assert result == [3]


@pytest.mark.asyncio
async def test_dispatcher_exception_isolation(caplog):
    from linch.observability import ObserverDispatcher, RunInfo

    second_called = []

    class RaisingObs:
        def on_run_start(self, info):
            raise ValueError("boom")

    class GoodObs:
        def on_run_start(self, info):
            second_called.append(True)

    hub = ObserverDispatcher([RaisingObs(), GoodObs()])
    info = RunInfo(run_id="r", session_id="s", model="m", prompt="p")
    with caplog.at_level(logging.ERROR, logger="linch.observability"):
        await hub.dispatch("on_run_start", info)

    # Exception should NOT propagate
    # Second observer should still run
    assert second_called == [True]
    # Error should be logged
    assert any("RaisingObs" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_dispatcher_partial_observer():
    """Observer that only implements on_event should not fail for other hooks."""
    from linch.observability import ObserverDispatcher, TurnInfo

    events_seen = []

    class EventOnlyObs:
        def on_event(self, event):
            events_seen.append(event)

    hub = ObserverDispatcher([EventOnlyObs()])
    # These hooks are not implemented — should be silently skipped
    await hub.dispatch("on_run_start", None)
    await hub.dispatch("on_turn_end", TurnInfo(run_id="r", turn_index=0))
    # on_event should work
    await hub.dispatch("on_event", "my-event")
    assert events_seen == ["my-event"]


def test_normalize_observers():
    from linch.observability import BaseObserver, normalize_observers

    obs = BaseObserver()
    assert normalize_observers(None) == []
    assert normalize_observers(obs) == [obs]
    assert normalize_observers([obs]) == [obs]
    assert normalize_observers((obs, obs)) == [obs, obs]


# ---------------------------------------------------------------------------
# SpanCollector integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_collector_full_span_tree():
    """Text-response run: collector captures run/turn/provider spans."""
    from linch.observability import SpanCollector

    collector = SpanCollector()
    agent = _make_agent(_make_text_provider(), hooks=_observe([collector]))
    session = await agent.session()
    await _collect(session)

    assert collector.run_span is not None, "no run span"
    turns = collector.spans_of("turn")
    assert len(turns) >= 1, "no turn spans"
    providers = collector.spans_of("provider")
    assert len(providers) >= 1, "no provider spans"


@pytest.mark.asyncio
async def test_run_end_once_on_success():
    from linch.observability import RunResultInfo

    run_ends: list[RunResultInfo] = []

    class CountObserver:
        def on_run_end(self, info):
            run_ends.append(info)

    agent = _make_agent(_make_text_provider(), hooks=_observe([CountObserver()]))
    session = await agent.session()
    await _collect(session)

    assert len(run_ends) == 1
    assert run_ends[0].subtype == "success"


@pytest.mark.asyncio
async def test_run_end_once_on_error():
    from linch.observability import RunResultInfo

    run_ends: list[RunResultInfo] = []
    run_starts = []

    class CountObserver:
        def on_run_start(self, info):
            run_starts.append(info)

        def on_run_end(self, info):
            run_ends.append(info)

    agent = _make_agent(_make_error_provider(), hooks=_observe([CountObserver()]))
    session = await agent.session()
    await _collect(session)

    assert len(run_starts) == 1
    assert len(run_ends) == 1
    assert run_ends[0].subtype == "error"
    assert run_ends[0].error is not None


@pytest.mark.asyncio
async def test_provider_and_turn_end_on_provider_error():
    calls = []

    class CountObserver:
        def on_turn_start(self, info):
            calls.append(("turn_start", info.turn_index))

        def on_turn_end(self, info):
            calls.append(("turn_end", info.turn_index))

        def on_provider_call_start(self, info):
            calls.append(("provider_start", info.turn_index))

        def on_provider_call_end(self, info):
            calls.append(("provider_end", info.turn_index, info.stop_reason))

    agent = _make_agent(_make_error_provider(), hooks=_observe([CountObserver()]))
    session = await agent.session()
    await _collect(session)

    assert calls == [
        ("turn_start", 0),
        ("provider_start", 0),
        ("provider_end", 0, "error"),
        ("turn_end", 0),
    ]


@pytest.mark.asyncio
async def test_turn_end_on_context_builder_error_before_provider_call():
    from linch.hooks import ContextInjectionHook

    calls = []

    class FailingContextBuilder:
        async def build(self, turn):
            raise RuntimeError("context failed")

    class CountObserver:
        def on_turn_start(self, info):
            calls.append(("turn_start", info.turn_index))

        def on_turn_end(self, info):
            calls.append(("turn_end", info.turn_index))

        def on_provider_call_start(self, info):
            calls.append(("provider_start", info.turn_index))

        def on_provider_call_end(self, info):
            calls.append(("provider_end", info.turn_index))

    agent = _make_agent(
        _make_text_provider(),
        hooks=[
            *_observe([CountObserver()]),
            ContextInjectionHook(FailingContextBuilder()),
        ],
    )
    session = await agent.session()
    await _collect(session)

    assert calls == [
        ("turn_start", 0),
        ("turn_end", 0),
    ]


@pytest.mark.asyncio
async def test_provider_span_timing():
    """Provider call span duration should be measurable and positive."""
    from linch.types import Usage

    class SlowProvider:
        id = "slow"

        def context_window(self, model):
            return 128_000

        async def stream(self, req):
            yield {"type": "message_start", "model": req.model}
            await asyncio.sleep(0.02)
            yield {"type": "text_delta", "text": "hi"}
            yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}

    from linch.observability import SpanCollector

    collector = SpanCollector()
    agent = _make_agent(SlowProvider(), hooks=_observe([collector]))
    session = await agent.session()
    await _collect(session)

    providers = collector.spans_of("provider")
    assert providers, "no provider spans"
    assert providers[0].duration_ms > 0


@pytest.mark.asyncio
async def test_tool_spans_present():
    """Tool spans should be emitted with tool_use_id, tool_name, duration_ms."""
    from linch.observability import SpanCollector

    collector = SpanCollector()
    provider = _make_tool_provider(tool_name="Echo", tool_input={"x": 1})
    agent = _make_agent(provider, hooks=_observe([collector]), tool_name="Echo")
    session = await agent.session()
    await _collect(session)

    tool_spans = collector.tool_spans
    assert len(tool_spans) >= 1, "no tool spans"
    ts = tool_spans[0]
    assert ts.tool_use_id != ""
    assert "Echo" in ts.name
    assert ts.duration_ms >= 0
    assert ts.attributes.get("tool_name") == "Echo"


@pytest.mark.asyncio
async def test_on_tool_end_receives_structured_tool_result():
    from linch.observability import ToolResultInfo
    from linch.tools import ToolResult

    tool_ends: list[ToolResultInfo] = []

    class ToolObserver:
        def on_tool_end(self, info):
            tool_ends.append(info)

    provider = _make_tool_provider(tool_name="Echo", tool_input={"x": 1})
    agent = _make_agent(
        provider,
        hooks=_observe([ToolObserver()]),
        tool_name="Echo",
        tool_result=ToolResult(
            content="echo-ok",
            summary="Echo summary",
            metadata={"trace": "abc"},
            truncated=True,
        ),
    )
    session = await agent.session()
    await _collect(session)

    assert tool_ends
    assert tool_ends[0].result == "echo-ok"
    assert tool_ends[0].tool_result is not None
    assert tool_ends[0].tool_result.summary == "Echo summary"
    assert tool_ends[0].tool_result.metadata == {"trace": "abc"}
    assert tool_ends[0].tool_result.truncated is True

    tool_result_messages = [
        message
        for message in session.provider_view
        if message.role == "user" and message.content and message.content[0].type == "tool_result"
    ]
    assert tool_result_messages
    provider_block = tool_result_messages[0].content[0]
    assert provider_block.content == "echo-ok"
    assert provider_block.is_error is False
    assert not hasattr(provider_block, "metadata")


@pytest.mark.asyncio
async def test_observer_exception_does_not_break_run():
    """A faulty observer that raises on every hook must not crash the run."""
    from linch.events import ResultEvent

    class AlwaysRaise:
        def on_run_start(self, info):
            raise RuntimeError("always fail")

        def on_run_end(self, info):
            raise RuntimeError("always fail")

        def on_turn_start(self, info):
            raise RuntimeError("always fail")

        def on_turn_end(self, info):
            raise RuntimeError("always fail")

        def on_provider_call_start(self, info):
            raise RuntimeError("always fail")

        def on_provider_call_end(self, info):
            raise RuntimeError("always fail")

        def on_event(self, event):
            raise RuntimeError("always fail")

    agent = _make_agent(_make_text_provider(), hooks=_observe([AlwaysRaise()]))
    session = await agent.session()
    events = await _collect(session)

    results = [e for e in events if isinstance(e, ResultEvent)]
    assert results, "run should complete with a ResultEvent"
    assert results[-1].subtype == "success"


@pytest.mark.asyncio
async def test_logging_observer_emits_lines(caplog):
    """LoggingObserver should emit at least one log line per key span."""
    from linch.observability import LoggingObserver

    obs = LoggingObserver(level=logging.DEBUG)
    agent = _make_agent(_make_text_provider(), hooks=_observe([obs]))
    session = await agent.session()
    with caplog.at_level(logging.DEBUG, logger="linch.observability"):
        await _collect(session)

    messages = [r.message for r in caplog.records]
    assert any("run.start" in m for m in messages), f"no run.start line: {messages}"
    assert any("run.end" in m for m in messages), f"no run.end line: {messages}"
    assert any("llm.end" in m for m in messages), f"no llm.end line: {messages}"


@pytest.mark.asyncio
async def test_on_event_receives_all_events():
    """on_event should receive every Event yielded by the loop."""
    from linch.events import ResultEvent

    all_events: list[Any] = []

    class EventCollector:
        def on_event(self, event):
            all_events.append(event)

    agent = _make_agent(_make_text_provider(), hooks=_observe([EventCollector()]))
    session = await agent.session()
    emitted_events = await _collect(session)

    # Every event emitted by run() should also appear in on_event
    result_events = [e for e in emitted_events if isinstance(e, ResultEvent)]
    assert result_events, "no ResultEvent emitted"
    # on_event should have received at least the same ResultEvent
    result_events_obs = [e for e in all_events if isinstance(e, ResultEvent)]
    assert result_events_obs, "on_event did not receive ResultEvent"


@pytest.mark.asyncio
async def test_multiple_observers_all_called():
    """All observers in the list should receive each hook."""

    ends: list[str] = []

    class Obs1:
        def on_run_end(self, info):
            ends.append("obs1")

    class Obs2:
        def on_run_end(self, info):
            ends.append("obs2")

    agent = _make_agent(_make_text_provider(), hooks=_observe([Obs1(), Obs2()]))
    session = await agent.session()
    await _collect(session)
    assert "obs1" in ends
    assert "obs2" in ends


# ---------------------------------------------------------------------------
# OTel tests — gated on opentelemetry availability
# ---------------------------------------------------------------------------


def test_otel_observer_missing_dep(monkeypatch):
    """OpenTelemetryObserver raises ProviderError when opentelemetry not installed."""
    import sys

    from linch.errors import ProviderError

    monkeypatch.setitem(sys.modules, "opentelemetry", None)  # type: ignore[arg-type]
    monkeypatch.setitem(sys.modules, "opentelemetry.trace", None)  # type: ignore[arg-type]

    # Re-import with patched sys.modules
    import importlib

    import linch.observability.otel as otel_mod

    importlib.reload(otel_mod)

    with pytest.raises(ProviderError, match="opentelemetry"):
        otel_mod.OpenTelemetryObserver()


@pytest.mark.asyncio
async def test_otel_span_tree():
    """OTel spans should nest run → turn → provider/tool with expected attributes."""
    pytest.importorskip("opentelemetry")
    from opentelemetry.sdk.trace import TracerProvider  # type: ignore[import]
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # type: ignore[import]
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # type: ignore[import]
        InMemorySpanExporter,
    )

    from linch.observability import OpenTelemetryObserver

    exporter = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = tp.get_tracer("test")

    obs = OpenTelemetryObserver(tracer=tracer)
    provider = _make_tool_provider(tool_name="Echo")
    agent = _make_agent(provider, hooks=_observe([obs]), tool_name="Echo")
    session = await agent.session()
    await _collect(session)

    spans = exporter.get_finished_spans()
    names = [s.name for s in spans]

    assert "agent.run" in names, f"missing agent.run in {names}"
    assert "agent.turn" in names, f"missing agent.turn in {names}"
    assert "gen_ai.chat" in names, f"missing gen_ai.chat in {names}"
    assert "execute_tool" in names, f"missing execute_tool in {names}"

    run_span = next(s for s in spans if s.name == "agent.run")
    assert run_span.attributes.get("gen_ai.request.model") == "test-model"


def test_otel_run_end_detaches_leftover_turn_tokens():
    """on_run_end must clean up turn context tokens for turns that never ended.

    Regression: when a turn is aborted, on_turn_end never fires, so the entry in
    `_turn_ctx_tokens` and its attached OTel context token leaked across runs.
    """
    pytest.importorskip("opentelemetry")
    from opentelemetry.sdk.trace import TracerProvider  # type: ignore[import]

    from linch.observability import OpenTelemetryObserver
    from linch.observability.protocol import RunInfo, RunResultInfo, TurnInfo
    from linch.types import Usage

    tracer = TracerProvider().get_tracer("test")
    obs = OpenTelemetryObserver(tracer=tracer)

    # Spy on detach to confirm leftover tokens are actually detached.
    import opentelemetry.context as _ctx

    detached: list[Any] = []
    orig_detach = _ctx.detach

    def _spy_detach(token):
        detached.append(token)
        return orig_detach(token)

    _ctx.detach = _spy_detach  # type: ignore[assignment]
    try:
        run_id = "run-abort"
        obs.on_run_start(RunInfo(run_id=run_id, session_id="s1", model="test-model", prompt="p"))
        obs.on_turn_start(TurnInfo(run_id=run_id, turn_index=0))

        # Turn token is stored; on_turn_end is NEVER called (aborted turn).
        leftover_token = obs._turn_ctx_tokens[(run_id, 0)]
        assert leftover_token is not None

        obs.on_run_end(
            RunResultInfo(
                run_id=run_id,
                session_id="s1",
                subtype="aborted",
                stop_reason="aborted",
                total_usage=Usage(),
                duration_ms=1,
            )
        )

        # No leftover turn token entry for this run.
        assert not [k for k in obs._turn_ctx_tokens if k[0] == run_id]
        # The leftover turn token was detached.
        assert leftover_token in detached
    finally:
        _ctx.detach = orig_detach  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# GenAI semantic conventions — provider_id threading + gen_ai.* attributes
# ---------------------------------------------------------------------------


def test_protocol_infos_default_provider_id():
    """provider_id is additive with a default so existing constructions keep working."""
    from linch.observability import ProviderCallInfo, ProviderCallResult, RunInfo
    from linch.types import Usage

    run = RunInfo(run_id="r", session_id="s", model="m", prompt="p")
    call = ProviderCallInfo(run_id="r", turn_index=0, model="m")
    result = ProviderCallResult(
        run_id="r", turn_index=0, model="m", stop_reason="end_turn", usage=Usage(), duration_ms=1
    )
    assert run.provider_id == ""
    assert call.provider_id == ""
    assert result.provider_id == ""

    run_x = RunInfo(run_id="r", session_id="s", model="m", prompt="p", provider_id="x")
    assert run_x.provider_id == "x"
    call_x = ProviderCallInfo(run_id="r", turn_index=0, model="m", provider_id="x")
    assert call_x.provider_id == "x"


@pytest.mark.asyncio
async def test_run_telemetry_hook_threads_provider_id():
    """RunTelemetryHook sources provider_id from the session's agent provider."""
    from linch.observability import BaseObserver

    class _CaptureObserver(BaseObserver):
        def __init__(self):
            self.run_infos = []
            self.call_infos = []
            self.call_results = []

        def on_run_start(self, info):
            self.run_infos.append(info)

        def on_provider_call_start(self, info):
            self.call_infos.append(info)

        def on_provider_call_end(self, info):
            self.call_results.append(info)

    obs = _CaptureObserver()
    agent = _make_agent(_make_tool_provider(), hooks=_observe([obs]))
    session = await agent.session()
    await _collect(session)

    assert obs.run_infos and all(i.provider_id == "fake-tool" for i in obs.run_infos)
    assert obs.call_infos and all(i.provider_id == "fake-tool" for i in obs.call_infos)
    assert obs.call_results and all(i.provider_id == "fake-tool" for i in obs.call_results)


def _make_otel_exporter():
    from opentelemetry.sdk.trace import TracerProvider  # type: ignore[import]
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # type: ignore[import]
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # type: ignore[import]
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, tp.get_tracer("test")


@pytest.mark.asyncio
async def test_otel_semconv_attributes_per_span_kind():
    """Every span kind carries the GenAI semconv attributes alongside linch.*."""
    pytest.importorskip("opentelemetry")
    from linch.observability import OpenTelemetryObserver

    exporter, tracer = _make_otel_exporter()
    obs = OpenTelemetryObserver(tracer=tracer)
    provider = _make_tool_provider(tool_name="Echo")
    agent = _make_agent(provider, hooks=_observe([obs]), tool_name="Echo")
    session = await agent.session()
    await _collect(session)

    spans = exporter.get_finished_spans()

    run_span = next(s for s in spans if s.name == "agent.run")
    assert run_span.attributes.get("gen_ai.operation.name") == "invoke_agent"
    assert run_span.attributes.get("gen_ai.conversation.id") == session.id
    assert run_span.attributes.get("gen_ai.provider.name") == "fake-tool"
    # linch.* attributes unchanged.
    assert run_span.attributes.get("linch.run_id")
    assert run_span.attributes.get("linch.session_id") == session.id

    chat_spans = [s for s in spans if s.name == "gen_ai.chat"]
    assert chat_spans
    finish_reasons = []
    for chat in chat_spans:
        assert chat.attributes.get("gen_ai.operation.name") == "chat"
        assert chat.attributes.get("gen_ai.provider.name") == "fake-tool"
        reasons = chat.attributes.get("gen_ai.response.finish_reasons")
        assert isinstance(reasons, (list, tuple)), f"finish_reasons must be a sequence: {reasons!r}"
        finish_reasons.extend(reasons)
    assert "tool_use" in finish_reasons and "end_turn" in finish_reasons

    tool_span = next(s for s in spans if s.name == "execute_tool")
    assert tool_span.attributes.get("gen_ai.operation.name") == "execute_tool"
    assert tool_span.attributes.get("gen_ai.tool.name") == "Echo"
    assert tool_span.attributes.get("gen_ai.tool.call.id") == "t1"
    # linch.tool.* attributes unchanged.
    assert tool_span.attributes.get("linch.tool.name") == "Echo"
    assert tool_span.attributes.get("linch.tool.use_id") == "t1"


@pytest.mark.asyncio
async def test_otel_cache_token_attributes():
    """Cache token usage is emitted only when non-zero."""
    pytest.importorskip("opentelemetry")
    from linch.observability import OpenTelemetryObserver
    from linch.types import Usage

    class CacheProvider:
        id = "fake-cache"

        def context_window(self, model):
            return 128_000

        async def stream(self, req):
            yield {"type": "message_start", "model": req.model}
            yield {"type": "text_delta", "text": "pong"}
            yield {
                "type": "message_end",
                "stop_reason": "end_turn",
                "usage": Usage(
                    input_tokens=10,
                    output_tokens=5,
                    cache_read_tokens=7,
                    cache_creation_tokens=3,
                ),
            }

    exporter, tracer = _make_otel_exporter()
    obs = OpenTelemetryObserver(tracer=tracer)
    agent = _make_agent(CacheProvider(), hooks=_observe([obs]))
    session = await agent.session()
    await _collect(session)

    chat = next(s for s in exporter.get_finished_spans() if s.name == "gen_ai.chat")
    assert chat.attributes.get("gen_ai.usage.cache_read_input_tokens") == 7
    assert chat.attributes.get("gen_ai.usage.cache_creation_input_tokens") == 3

    # Zero-cache run: the cache attributes are absent, not zero.
    exporter2, tracer2 = _make_otel_exporter()
    obs2 = OpenTelemetryObserver(tracer=tracer2)
    agent2 = _make_agent(_make_text_provider(), hooks=_observe([obs2]))
    session2 = await agent2.session()
    await _collect(session2)

    chat2 = next(s for s in exporter2.get_finished_spans() if s.name == "gen_ai.chat")
    assert "gen_ai.usage.cache_read_input_tokens" not in chat2.attributes
    assert "gen_ai.usage.cache_creation_input_tokens" not in chat2.attributes


def test_otel_provider_name_well_known_mapping():
    """Provider ids map to semconv well-known values; unknown ids pass through."""
    pytest.importorskip("opentelemetry")
    from linch.observability import OpenTelemetryObserver, ProviderCallInfo, ProviderCallResult
    from linch.types import Usage

    expected = {
        "anthropic": "anthropic",
        "openai-chat": "openai",
        "openai-responses": "openai",
        "gemini": "gcp.gemini",
        "vllm": "vllm",  # self-hosted runtimes pass through, never merged
        "sglang": "sglang",
        "llamacpp": "llamacpp",
    }

    exporter, tracer = _make_otel_exporter()
    obs = OpenTelemetryObserver(tracer=tracer)
    for turn, provider_id in enumerate(expected):
        obs.on_provider_call_start(
            ProviderCallInfo(run_id="r", turn_index=turn, model="m", provider_id=provider_id)
        )
        obs.on_provider_call_end(
            ProviderCallResult(
                run_id="r",
                turn_index=turn,
                model="m",
                stop_reason="end_turn",
                usage=Usage(),
                duration_ms=1,
            )
        )
    # Empty provider_id → attribute absent.
    obs.on_provider_call_start(ProviderCallInfo(run_id="r", turn_index=99, model="m"))
    obs.on_provider_call_end(
        ProviderCallResult(
            run_id="r",
            turn_index=99,
            model="m",
            stop_reason="end_turn",
            usage=Usage(),
            duration_ms=1,
        )
    )

    spans = {s.attributes.get("linch.turn_index"): s for s in exporter.get_finished_spans()}
    for turn, provider_id in enumerate(expected):
        actual = spans[turn].attributes.get("gen_ai.provider.name")
        assert actual == expected[provider_id], provider_id
    assert "gen_ai.provider.name" not in spans[99].attributes
