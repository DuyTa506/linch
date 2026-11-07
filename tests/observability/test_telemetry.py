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


async def test_tool_content_capture_uses_post_tool_redaction(otel, monkeypatch):
    """Export the PostToolUse result rather than the pre-hook execution value."""
    from linch import RedactionConfig, RedactionHook, RedactionRule, tool
    from tests.support.agents import make_agent
    from tests.support.providers import ScriptedProvider, TextTurn, ToolUseTurn
    from tests.support.tools import tool_registry

    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "true")
    context, exporter, _ = otel

    @tool(scope="read")
    def read_secret() -> str:
        """Return a value that the post-tool policy redacts."""
        return "SECRET_VALUE"

    provider = ScriptedProvider([ToolUseTurn("read_secret", {}), TextTurn("done")])
    agent = make_agent(
        provider,
        context=context,
        tools=tool_registry(read_secret),
        hooks=[
            RedactionHook(RedactionConfig(rules=(RedactionRule("SECRET_VALUE", "[REDACTED]"),)))
        ],
    )
    session = await agent.session()
    try:
        events = [event async for event in session.run("prompt")]
        assert events[-1].subtype == "success"
        span = next(
            span
            for span in exporter.get_finished_spans()
            if span.attributes.get("gen_ai.operation.name") == "execute_tool"
        )
        assert span.attributes["gen_ai.tool.call.result"] == "[REDACTED]"
        assert "SECRET_VALUE" not in str(span.attributes)
    finally:
        await agent.close()


async def test_prompt_content_capture_uses_user_prompt_submit_redaction(otel, monkeypatch):
    """Export the UserPromptSubmit prompt after hooks have redacted it."""
    import json

    from linch import RedactionConfig, RedactionHook, RedactionRule
    from tests.support.agents import make_agent
    from tests.support.providers import ScriptedProvider, TextTurn

    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "true")
    context, exporter, _ = otel
    agent = make_agent(
        ScriptedProvider([TextTurn("done")]),
        context=context,
        hooks=[
            RedactionHook(
                RedactionConfig(
                    redact_user_prompt=True,
                    rules=(RedactionRule("SECRET_VALUE", "[REDACTED]"),),
                )
            )
        ],
    )
    session = await agent.session()
    try:
        events = [event async for event in session.run("SECRET_VALUE")]
        assert events[-1].subtype == "success"
        span = next(
            span
            for span in exporter.get_finished_spans()
            if span.attributes.get("gen_ai.operation.name") == "invoke_agent"
        )
        assert json.loads(span.attributes["gen_ai.input.messages"]) == "[REDACTED]"
        assert "SECRET_VALUE" not in str(span.attributes)
    finally:
        await agent.close()


@pytest.mark.parametrize("redact_first", [False, True])
@pytest.mark.parametrize("action", ["block", "stop"])
async def test_rejected_prompt_is_not_captured(otel, monkeypatch, redact_first, action):
    """Rejection can short-circuit redaction, so omit rejected prompt content."""
    from linch import HookResult, RedactionConfig, RedactionHook, RedactionRule
    from tests.support.agents import make_agent
    from tests.support.providers import ScriptedProvider, TextTurn

    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "true")
    context, exporter, _ = otel

    class BlockPrompt:
        resume_policy_id = "test.block_prompt"

        def on_user_prompt_submit(self, ctx):
            return getattr(HookResult, action)("blocked")

    redactor = RedactionHook(
        RedactionConfig(
            redact_user_prompt=True,
            rules=(RedactionRule("SECRET_VALUE", "[REDACTED]"),),
        )
    )
    agent = make_agent(
        ScriptedProvider([TextTurn("unreachable")]),
        context=context,
        hooks=[redactor, BlockPrompt()] if redact_first else [BlockPrompt(), redactor],
    )
    session = await agent.session()
    try:
        events = [event async for event in session.run("SECRET_VALUE")]
        assert events[-1].subtype == "error"
        span = next(
            span
            for span in exporter.get_finished_spans()
            if span.attributes.get("gen_ai.operation.name") == "invoke_agent"
        )
        assert "SECRET_VALUE" not in str(span.attributes)
        assert "gen_ai.input.messages" not in span.attributes
    finally:
        await agent.close()


async def test_start_run_span_does_not_capture_prompt_before_hooks(otel, monkeypatch):
    """Run creation failures cannot export a prompt that hooks never processed."""
    from types import SimpleNamespace

    from linch._telemetry import start_run_span

    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "true")
    context, exporter, _ = otel
    agent = SimpleNamespace(
        context=context,
        provider=SimpleNamespace(id="test-provider"),
        model="test-model",
    )
    session = SimpleNamespace(id="session", agent=agent)
    span = start_run_span(session, "SECRET_VALUE", "run")
    span.end()
    recorded = exporter.get_finished_spans()[0]
    assert "gen_ai.input.messages" not in recorded.attributes
    assert "SECRET_VALUE" not in str(recorded.attributes)


async def test_closing_run_drains_deferred_tool_spans(otel, monkeypatch):
    """Closing the run iterator ends tool spans waiting for PostToolUse."""
    from types import SimpleNamespace

    from linch._telemetry import in_span, telemetry_for, trace_tool

    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "true")
    context, exporter, _ = otel
    agent = SimpleNamespace(context=context)
    session = SimpleNamespace(id="session", agent=agent, active_run_id="run", _telemetry_run=None)
    telemetry = telemetry_for(agent)
    run_span = telemetry.tracer.start_span("invoke_agent")
    call = SimpleNamespace(id="tool", block=SimpleNamespace(name="echo"), input={})
    outcome = SimpleNamespace(block=SimpleNamespace(content="raw", is_error=False), error_type=None)

    @trace_tool
    async def execute(*args, **kwargs):
        return outcome

    async def events():
        await execute(call, None, agent, session, None, run_id="run")
        yield SimpleNamespace(type="system")

    iterator = in_span(events(), run_span, session=session, run_id="run")
    await anext(iterator)
    await iterator.aclose()
    assert any(
        span.attributes.get("gen_ai.operation.name") == "execute_tool"
        for span in exporter.get_finished_spans()
    )


async def test_detached_tool_span_uses_originating_run_state(otel, monkeypatch):
    """Background completion must close the span for its captured run."""
    from types import SimpleNamespace

    from linch._telemetry import (
        _RunTrace,
        record_tool_result,
        telemetry_for,
        trace_tool,
    )

    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "true")
    context, exporter, _ = otel
    agent = SimpleNamespace(context=context)
    session = SimpleNamespace(id="session", agent=agent, active_run_id="B", _telemetry_run=None)
    telemetry = telemetry_for(agent)
    span_a = telemetry.tracer.start_span("invoke_agent A")
    span_b = telemetry.tracer.start_span("invoke_agent B")
    state_a = _RunTrace(span_a, "A")
    state_b = _RunTrace(span_b, "B")
    telemetry._runs[(session.id, "A")] = state_a
    session._telemetry_run = state_b
    call = SimpleNamespace(
        id="tool",
        _run_id="A",
        block=SimpleNamespace(name="echo"),
        input={},
    )
    outcome = SimpleNamespace(block=SimpleNamespace(content="raw", is_error=False), error_type=None)

    @trace_tool
    async def execute(*args, **kwargs):
        return outcome

    await execute(call, None, agent, session, None, run_id="A")
    assert len(state_a.tool_spans) == 1
    assert not state_b.tool_spans
    record_tool_result(session, call.id, "redacted", run_id="A")
    assert not state_a.tool_spans
    assert any(
        span.attributes.get("gen_ai.operation.name") == "execute_tool"
        and span.attributes.get("gen_ai.tool.call.result") == "redacted"
        for span in exporter.get_finished_spans()
    )
    span_a.end()
    span_b.end()


async def test_overlapping_detached_tools_keep_origin_registry_until_both_finish(otel, monkeypatch):
    """One completed background tool must not retire a sibling's run registry."""
    from types import SimpleNamespace

    from linch._telemetry import _RunTrace, record_tool_result, telemetry_for, trace_tool

    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "true")
    context, exporter, _ = otel
    agent = SimpleNamespace(context=context)
    pending_tasks = [SimpleNamespace(done=lambda: False), SimpleNamespace(done=lambda: False)]
    session = SimpleNamespace(
        id="session",
        agent=agent,
        active_run_id="B",
        _telemetry_run=None,
        background_tasks=pending_tasks,
    )
    telemetry = telemetry_for(agent)
    owner_span = telemetry.tracer.start_span("invoke_agent A")
    owner = _RunTrace(owner_span, "A")
    owner.closed = True
    owner.background_tasks = list(pending_tasks)
    telemetry._runs[(session.id, "A")] = owner

    @trace_tool
    async def execute(call, *_args, **_kwargs):
        return SimpleNamespace(
            block=SimpleNamespace(content=f"raw-{call.id}", is_error=False), error_type=None
        )

    def call(call_id):
        return SimpleNamespace(
            id=call_id,
            _run_id="A",
            block=SimpleNamespace(name="echo"),
            input={},
        )

    first, second = call("first"), call("second")
    await execute(first, None, agent, session, None, run_id="A")
    record_tool_result(session, first.id, "redacted-first", run_id="A")
    await execute(second, None, agent, session, None, run_id="A")
    assert len(owner.tool_spans) == 1
    record_tool_result(session, second.id, "redacted-second", run_id="A")
    assert (session.id, "A") in telemetry._runs
    pending_tasks[0].done = lambda: True
    pending_tasks[1].done = lambda: True
    record_tool_result(session, "no-span", "ignored", run_id="A")
    assert (session.id, "A") not in telemetry._runs
    assert {
        span.attributes.get("gen_ai.tool.call.result")
        for span in exporter.get_finished_spans()
        if span.attributes.get("gen_ai.operation.name") == "execute_tool"
    } == {"redacted-first", "redacted-second"}
    owner_span.end()


async def test_closed_run_cleanup_ignores_later_run_background_tasks(otel, monkeypatch):
    """Run A cleanup must not wait on background work admitted by run B."""
    from types import SimpleNamespace

    from linch._telemetry import _RunTrace, record_tool_result, telemetry_for

    monkeypatch.delenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", raising=False)
    context, _, _ = otel
    agent = SimpleNamespace(context=context)
    task_a = SimpleNamespace(done=lambda: False)
    task_b = SimpleNamespace(done=lambda: False)
    session = SimpleNamespace(
        id="session",
        agent=agent,
        active_run_id="B",
        background_tasks=[task_a, task_b],
        _telemetry_run=None,
    )
    telemetry = telemetry_for(agent)
    owner_span = telemetry.tracer.start_span("invoke_agent A")
    owner = _RunTrace(owner_span, "A")
    owner.closed = True
    owner.background_tasks = [task_a]
    telemetry._runs[(session.id, "A")] = owner

    task_a.done = lambda: True
    record_tool_result(session, "no-span", "ignored", run_id="A")
    assert (session.id, "A") not in telemetry._runs
    assert (session.id, "B") not in telemetry._runs
    owner_span.end()


@pytest.mark.parametrize("capture", [False, True])
async def test_completed_runs_do_not_retain_earlier_run_background_tasks(
    otel, monkeypatch, capture
):
    """A long-lived tool must retain only its originating run's telemetry."""
    from linch import tool
    from linch._telemetry import telemetry_for
    from tests.support.agents import make_agent
    from tests.support.providers import ScriptedProvider, TextTurn, ToolUseTurn
    from tests.support.tools import tool_registry

    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", str(capture).lower())
    context, exporter, _ = otel
    release = asyncio.Event()
    started = asyncio.Event()

    @tool(scope="read")
    async def slow() -> str:
        """Wait until later runs have finished."""
        started.set()
        await release.wait()
        return "tool-finished"

    agent = make_agent(
        ScriptedProvider(
            [
                ToolUseTurn("slow", {"run_in_background": True}),
                TextTurn("A"),
                TextTurn("B"),
                TextTurn("C"),
            ]
        ),
        context=context,
        tools=tool_registry(slow),
        enable_background_tools=True,
    )
    session = await agent.session()
    telemetry = telemetry_for(agent)
    try:
        first = [event async for event in session.run("A")]
        assert first[-1].subtype == "success"
        await asyncio.wait_for(started.wait(), timeout=1)
        assert len(telemetry._runs) == 1
        origin = next(iter(telemetry._runs))
        for prompt in ("B", "C"):
            events = [event async for event in session.run(prompt)]
            assert events[-1].subtype == "success"
            assert set(telemetry._runs) == {origin}
        release.set()
        await asyncio.gather(*session.background_tasks)
        assert not telemetry._runs
        if capture:
            tool_span = next(
                span
                for span in exporter.get_finished_spans()
                if span.attributes.get("gen_ai.operation.name") == "execute_tool"
            )
            assert tool_span.attributes["linch.run_id"] == origin[1]
            assert tool_span.attributes["gen_ai.tool.call.result"] == "tool-finished"
    finally:
        release.set()
        await asyncio.gather(*session.background_tasks, return_exceptions=True)
        await agent.close()


@pytest.mark.parametrize("termination", ["cancel", "failure"])
async def test_detached_post_tool_failure_closes_only_its_pending_span(
    otel, monkeypatch, termination
):
    """Interrupted post-processing must close its span without capturing raw output."""
    from linch import RedactionConfig, RedactionHook, RedactionRule, scheduler, tool
    from linch._telemetry import telemetry_for
    from tests.support.agents import make_agent
    from tests.support.providers import ScriptedProvider, TextTurn, ToolUseTurn
    from tests.support.tools import tool_registry

    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "true")
    context, exporter, _ = otel
    release_tools = asyncio.Event()
    entered = {name: asyncio.Event() for name in ("first", "second")}
    release_hooks = {name: asyncio.Event() for name in entered}
    hook_tasks = {}

    class WaitHook:
        resume_policy_id = "test.wait_post_tool"

        async def on_post_tool_use(self, ctx):
            name = ctx.input["name"]
            hook_tasks[name] = asyncio.current_task()
            entered[name].set()
            await release_hooks[name].wait()

    @tool(scope="read")
    async def slow(name: str) -> str:
        """Finish execution only after the foreground run has closed."""
        await release_tools.wait()
        return "SECRET_VALUE"

    original_offload = scheduler._maybe_offload_block

    async def failing_offload(*args, **kwargs):
        if kwargs["call"].id == "first" and entered["first"].is_set():
            raise RuntimeError("post-tool projection failed")
        return await original_offload(*args, **kwargs)

    if termination == "failure":
        monkeypatch.setattr(scheduler, "_maybe_offload_block", failing_offload)

    agent = make_agent(
        ScriptedProvider(
            [
                ToolUseTurn("slow", {"name": "first", "run_in_background": True}, "first"),
                ToolUseTurn("slow", {"name": "second", "run_in_background": True}, "second"),
                TextTurn("done"),
            ]
        ),
        context=context,
        tools=tool_registry(slow),
        enable_background_tools=True,
        hooks=[
            WaitHook(),
            RedactionHook(RedactionConfig(rules=(RedactionRule("SECRET_VALUE", "[REDACTED]"),))),
        ],
    )
    session = await agent.session()
    telemetry = telemetry_for(agent)
    try:
        events = [event async for event in session.run("prompt")]
        assert events[-1].subtype == "success"
        release_tools.set()
        for barrier in entered.values():
            await asyncio.wait_for(barrier.wait(), timeout=1)
        owner = next(iter(telemetry._runs.values()))
        assert set(owner.tool_spans) == {"first", "second"}
        if termination == "cancel":
            hook_tasks["first"].cancel()
        else:
            release_hooks["first"].set()
        first_result = await asyncio.gather(hook_tasks["first"], return_exceptions=True)
        assert isinstance(
            first_result[0], asyncio.CancelledError if termination == "cancel" else RuntimeError
        )
        assert set(owner.tool_spans) == {"second"}
        assert owner.tool_spans["second"][0].is_recording()
        assert len(telemetry._runs) == 1
        release_hooks["second"].set()
        await hook_tasks["second"]
        assert not telemetry._runs
        tool_spans = [
            span
            for span in exporter.get_finished_spans()
            if span.attributes.get("gen_ai.operation.name") == "execute_tool"
        ]
        assert len(tool_spans) == 2
        first_span = next(
            span for span in tool_spans if span.attributes["gen_ai.tool.call.id"] == "first"
        )
        second_span = next(
            span for span in tool_spans if span.attributes["gen_ai.tool.call.id"] == "second"
        )
        assert "gen_ai.tool.call.result" not in first_span.attributes
        assert second_span.attributes["gen_ai.tool.call.result"] == "[REDACTED]"
        assert "SECRET_VALUE" not in str([span.attributes for span in tool_spans])
        await agent.close()
        assert not telemetry._runs
    finally:
        release_tools.set()
        for barrier in release_hooks.values():
            barrier.set()
        await asyncio.gather(*session.background_tasks, return_exceptions=True)
        await agent.close()


async def test_record_tool_result_ignores_unbound_scheduler_session():
    """Telemetry cleanup is optional for lightweight scheduler sessions."""
    from types import SimpleNamespace

    from linch._telemetry import record_tool_result, register_background_task

    record_tool_result(SimpleNamespace(id="session", active_run_id="run"), "tool", "result")
    register_background_task(
        SimpleNamespace(id="session"), object(), run_id="run", tool_use_id="tool"
    )


async def test_capture_off_releases_closed_run_registry_after_background_tasks_finish(
    otel, monkeypatch
):
    """Capture-off runs must not retain a registry after detached work drains."""
    import asyncio
    from types import SimpleNamespace

    from linch._telemetry import in_span, register_background_task, telemetry_for

    monkeypatch.delenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", raising=False)
    context, _, _ = otel
    agent = SimpleNamespace(context=context)
    release = asyncio.Event()
    task = asyncio.create_task(release.wait())
    session = SimpleNamespace(
        id="session",
        agent=agent,
        active_run_id="run",
        _telemetry_run=None,
        background_tasks=[task],
    )
    telemetry = telemetry_for(agent)
    run_span = telemetry.tracer.start_span("invoke_agent")

    async def events():
        register_background_task(session, task, run_id="run", tool_use_id="tool")
        yield SimpleNamespace(type="system")

    iterator = in_span(events(), run_span, session=session, run_id="run")
    await anext(iterator)
    await iterator.aclose()
    assert (session.id, "run") in telemetry._runs
    release.set()
    await task
    await asyncio.sleep(0)
    assert (session.id, "run") not in telemetry._runs


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
