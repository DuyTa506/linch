"""Host-owned runner recipes (ROADMAP P7).

``LoopRunner.run_once()`` is the SDK's single primitive for one tick of recurring
work. Cron schedules, webhook handlers, fixed-interval loops, and CI gates are
**host lifecycle** concerns — Linch deliberately does not own them. These are
tiny wrappers showing how each host system drives ``run_once()``; the runtime
stays a library, not a daemon.

Each recipe is a plain function over a ``LoopRunner`` + ``LoopSpec`` so a host
can call it from cron, an HTTP route, a process loop, or a CI step. The
``__main__`` demo and the smoke test inject a fake provider, so this file runs
without credentials.

Run:
    python examples/recipes/runner_recipes.py
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from linch import (
    Agent,
    FileLoopArtifactStore,
    InMemoryLoopLeaseStore,
    LoopRunner,
    LoopSpec,
    LoopTickResult,
    LoopTrigger,
)
from linch.config import FeatureFlags
from linch.providers.base import BaseProvider
from linch.sessions import InMemorySessionStore
from linch.types import Usage


def build_runner(
    *,
    provider: Any,
    root: str,
    model: str = "m",
) -> tuple[LoopRunner, LoopSpec]:
    """Wire an agent + a ``LoopRunner`` for a single recurring domain.

    A real host builds this once at startup and reuses it across ticks. This
    recipe uses ``InMemoryLoopLeaseStore``, so overlap protection is
    single-process only; use a durable lease backend when multiple processes can
    tick the same spec. The artifact store carries durable loop state on disk.
    """
    agent = Agent(
        model=model,
        provider=provider,
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        cwd=root,
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
    )
    runner = LoopRunner(
        agent,
        artifacts=FileLoopArtifactStore(root=root),
        leases=InMemoryLoopLeaseStore(),
    )
    spec = LoopSpec(
        id="inbox-triage",
        charter="Triage the work queue once per tick.",
        prompt="Do one unit of work, then report what you did.",
    )
    return runner, spec


# ── Recipe 1: cron ────────────────────────────────────────────────────────────
# A crontab line runs a one-shot script:
#   */15 * * * * /usr/bin/python -m myapp.tick
# That script builds the runner and calls this once, then exits.


async def cron_tick(runner: LoopRunner, spec: LoopSpec) -> LoopTickResult:
    return await runner.run_once(spec, LoopTrigger(source="cron"))


# ── Recipe 2: webhook ─────────────────────────────────────────────────────────
# An HTTP handler (FastAPI/Flask/...) calls run_once() with the request body as
# the trigger payload, then returns the tick outcome:
#   @app.post("/triage")
#   async def triage(req: Request):
#       result = await webhook_tick(runner, spec, await req.body())
#       return {"status": result.status, "done": result.done}


async def webhook_tick(runner: LoopRunner, spec: LoopSpec, payload: str) -> LoopTickResult:
    return await runner.run_once(spec, LoopTrigger(source="webhook", payload=payload))


# ── Recipe 3: fixed interval ──────────────────────────────────────────────────
# A long-lived host process ticks on a timer. The host owns the loop, the sleep,
# and the shutdown signal — the SDK just runs each tick.


async def fixed_interval(
    runner: LoopRunner,
    spec: LoopSpec,
    *,
    ticks: int,
    interval_s: float,
) -> list[LoopTickResult]:
    results: list[LoopTickResult] = []
    for _ in range(ticks):
        results.append(await runner.run_once(spec, LoopTrigger(source="interval")))
        if len(results) < ticks:
            await asyncio.sleep(interval_s)
    return results


# ── Recipe 4: CI gate ─────────────────────────────────────────────────────────
# A CI step runs one tick and maps the outcome to a process exit code, so a
# failing run fails the pipeline.


async def ci_gate(runner: LoopRunner, spec: LoopSpec) -> int:
    result = await runner.run_once(spec, LoopTrigger(source="ci"))
    ok = result.status == "completed"
    print(f"[ci] tick status={result.status} done={result.done} -> exit {0 if ok else 1}")
    return 0 if ok else 1


class _AlwaysDoneProvider(BaseProvider):
    """A fake provider that completes every tick immediately (no credentials)."""

    id = "recipe-fake"

    def context_window(self, model: str) -> int:
        return 128_000

    async def stream(self, req: Any) -> AsyncIterator[dict[str, Any]]:
        yield {"type": "message_start", "model": req.model}
        yield {"type": "text_delta", "text": "tick complete: queue empty"}
        yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}


async def main() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as root:
        runner, spec = build_runner(provider=_AlwaysDoneProvider(), root=root)

        print("cron:")
        print("  ", (await cron_tick(runner, spec)).final_text)

        print("webhook:")
        print("  ", (await webhook_tick(runner, spec, payload="ticket#42")).final_text)

        print("fixed interval (3 ticks):")
        for tick in await fixed_interval(runner, spec, ticks=3, interval_s=0.0):
            print(f"   iteration {tick.iteration}: {tick.final_text}")

        print("ci gate:")
        code = await ci_gate(runner, spec)
        print("   exit code:", code)


if __name__ == "__main__":
    asyncio.run(main())
