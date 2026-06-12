"""Observability demo — shows LoggingObserver and optionally OpenTelemetryObserver.

Loads ../.env automatically when present.  Never prints secret values.

Run without OTel::

    python examples/observability_agent.py

Run with OTel console spans (requires pip install 'linch[otel]')::

    pip install 'linch[otel]'
    python examples/observability_agent.py
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

# ── Load .env from the project root (never overrides real env vars) ────────────

ROOT = Path(__file__).resolve().parents[1]
MODEL = os.environ.get("MODEL", "gpt-4o")


def load_project_env() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    with env_file.open() as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip("\"'")
            os.environ.setdefault(key, val)


load_project_env()

# ── Logging setup ──────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.DEBUG, format="%(name)s  %(levelname)s  %(message)s")
log = logging.getLogger("observability_demo")

# ── Build observer list ────────────────────────────────────────────────────────

from linch.hooks import RunTelemetryHook  # noqa: E402
from linch.observability import LoggingObserver  # noqa: E402

observers = [LoggingObserver(level=logging.INFO)]

# Attempt to attach OpenTelemetry with a console exporter.
try:
    from opentelemetry.sdk.trace import TracerProvider  # type: ignore[import]
    from opentelemetry.sdk.trace.export import (  # type: ignore[import]
        ConsoleSpanExporter,
        SimpleSpanProcessor,
    )

    from linch.observability import OpenTelemetryObserver

    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    otel_obs = OpenTelemetryObserver(tp.get_tracer("observability_demo"))
    observers.append(otel_obs)
    log.info("OpenTelemetry console exporter attached — spans will print below")
except ModuleNotFoundError:
    log.info("opentelemetry not installed — run `pip install 'linch[otel]'` for OTel spans")

# ── Create agent ───────────────────────────────────────────────────────────────

api_key = os.environ.get("OPENAI_API_KEY")
if not api_key:
    log.warning("OPENAI_API_KEY not set — skipping live run")
else:

    async def main() -> None:
        from linch import Agent
        from linch.config import FeatureFlags
        from linch.sessions import InMemorySessionStore
        from linch.tools.registry import empty_tools

        agent = Agent(
            model=MODEL,
            openai_api_key=api_key,
            tools=empty_tools(),
            permissions={"mode": "skip-dangerous"},
            session_store=InMemorySessionStore(),
            features=FeatureFlags(skills=False, subagents=False, mcp=False),
            loop_guard=None,
            hooks=[RunTelemetryHook(observers)],
        )
        session = await agent.session()
        async for _event in session.run("Reply with exactly: pong"):
            pass  # span hooks fire automatically; LoggingObserver handles logging

    asyncio.run(main())
