"""Runtime checks for host context propagation and optional SDK boundaries."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from opentelemetry import context as otel_context
from opentelemetry import propagate, trace


def _agent_for(context, *, provider=None):
    from linch import Agent, FeatureFlags, InMemorySessionStore
    from linch.evals import ScriptedProvider, TextTurn

    return Agent(
        model="test-model",
        provider=provider or ScriptedProvider([TextTurn("offline answer")]),
        context=context,
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        loop_guard=None,
    )


async def _collect(session, prompt="offline prompt"):
    return [event async for event in session.run(prompt)]


def _operation_spans(exporter, operation):
    return [
        span
        for span in exporter.get_finished_spans()
        if span.attributes.get("gen_ai.operation.name") == operation
    ]


async def test_traceparent_from_host_job_payload_parents_invoke_agent(otel):
    context, exporter, _ = otel
    agent = _agent_for(context)
    try:
        session = await agent.session()
        trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
        parent_span_id = "00f067aa0ba902b7"
        job_payload = {
            "traceparent": f"00-{trace_id}-{parent_span_id}-01",
        }

        extracted = propagate.extract(job_payload)
        extracted_parent = trace.get_current_span(extracted).get_span_context()
        token = otel_context.attach(extracted)
        try:
            events = await _collect(session, "job payload")
        finally:
            otel_context.detach(token)

        assert events[-1].subtype == "success"
        (run,) = _operation_spans(exporter, "invoke_agent")
        assert run.context.trace_id == extracted_parent.trace_id
        assert run.parent.span_id == extracted_parent.span_id
    finally:
        await agent.close()


async def test_cancelled_run_finishes_spans_and_restores_caller_context(otel):
    class BlockingProvider:
        id = "blocking"

        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.closed = asyncio.Event()

        def context_window(self, model):
            return 128_000

        async def stream(self, request):
            self.started.set()
            try:
                yield {"type": "message_start", "model": request.model}
                await self.release.wait()
            finally:
                self.closed.set()

    context, exporter, _ = otel
    provider = BlockingProvider()
    agent = _agent_for(context, provider=provider)
    session = await agent.session()

    async def consume():
        async for _ in session.run("cancel me"):
            pass

    host_tracer = context.tracer_provider.get_tracer("host")
    try:
        with host_tracer.start_as_current_span("host request") as host:
            task = asyncio.create_task(consume())
            await provider.started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert trace.get_current_span() is host

        assert provider.closed.is_set()
        assert not session._active
        assert session.active_run_id is None
        assert session._telemetry_run is None
        operations = {
            operation: _operation_spans(exporter, operation)[0]
            for operation in ("invoke_agent", "step", "chat")
        }
        assert all(
            span.status.status_code is trace.StatusCode.ERROR
            and span.attributes["error.type"] == "CancelledError"
            for span in operations.values()
        )
    finally:
        provider.release.set()
        await agent.close()


def test_offline_subprocess_does_not_import_sdk():
    source = textwrap.dedent(
        """
        import asyncio
        import sys

        from linch import Agent, FeatureFlags, InMemorySessionStore
        from linch.evals import ScriptedProvider, TextTurn


        async def main():
            agent = Agent(
                model="offline",
                provider=ScriptedProvider([TextTurn("subprocess answer")]),
                session_store=InMemorySessionStore(),
                features=FeatureFlags(skills=False, subagents=False, mcp=False),
                loop_guard=None,
            )
            try:
                session = await agent.session()
                events = [event async for event in session.run("offline")]
                assert events[-1].subtype == "success"
                assert events[-1].final_text == "subprocess answer"
            finally:
                await agent.close()
            assert "opentelemetry.sdk" not in sys.modules


        asyncio.run(main())
        """
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).parents[2] / "src")
    result = subprocess.run(
        [sys.executable, "-c", source],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
