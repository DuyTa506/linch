"""Direct OpenTelemetry setup for a Linch agent.

The Linch core only needs ``opentelemetry-api``. This example adds the SDK at
the host boundary, injects providers through the agent ``Context`` (without
changing process-global providers), and prints spans/metrics locally.

For production, replace the console exporters with an OTLP exporter pointed at
your collector. Langfuse, Grafana Tempo, and Datadog can all receive the same
OTLP data; the agent wiring stays unchanged.
"""

from __future__ import annotations

import asyncio
import os


async def main() -> None:
    try:
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import (
            ConsoleMetricExporter,
            PeriodicExportingMetricReader,
        )
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor
    except ModuleNotFoundError:
        print("Install opentelemetry-sdk to run this example (the Linch core uses the API only).")
        return

    from linch import Agent, Context
    from linch.config import FeatureFlags
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools

    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    meter_provider = MeterProvider(
        metric_readers=[
            PeriodicExportingMetricReader(ConsoleMetricExporter(), export_interval_millis=5_000)
        ]
    )

    # Do not call trace.set_tracer_provider()/metrics.set_meter_provider() here.
    # A host can inject a different provider into each Agent Context.
    host_context = Context()
    host_context.register("tracer_provider", tracer_provider)
    host_context.register("meter_provider", meter_provider)

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("Set OPENAI_API_KEY to run the live example.")
        tracer_provider.shutdown()
        meter_provider.shutdown()
        return

    agent = Agent(
        model=os.environ.get("MODEL", "gpt-4o"),
        openai_api_key=api_key,
        tools=empty_tools(),
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        loop_guard=None,
        context=host_context,
    )
    try:
        session = await agent.session()
        async for event in session.run("Reply with exactly: pong"):
            if event.type == "result":
                print(event.final_text)
    finally:
        await agent.close()
        # The host owns SDK providers and shuts them down after its agents.
        tracer_provider.shutdown()
        meter_provider.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
