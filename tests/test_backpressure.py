"""Event-stream backpressure contract (ROADMAP Phase 5.4).

`session.run()` is a plain async-generator chain: it `yield`s each event and is
suspended at the yield until the host consumer pulls the next one. So a slow
consumer throttles the whole producer — provider streaming and tool execution do
not race ahead of consumption. There is no unbounded internal queue buffering
events away from the consumer.

Verify: a consumer that pulls the first event and then pauses leaves the tool
*unexecuted* — production is suspended at the yield, not running ahead. Resuming
consumption lets it proceed.
"""

from __future__ import annotations

import asyncio
from typing import Any


def _agent(provider: Any, tools: Any) -> Any:
    from linch import Agent
    from linch.sessions import InMemorySessionStore

    return Agent(
        model="m",
        provider=provider,
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        cwd=".",
        tools=tools,
    )


async def test_slow_consumer_halts_the_producer() -> None:
    from linch.evals import ScriptedProvider, TextTurn, ToolUseTurn
    from linch.tools import ToolRegistry, tool

    ran = {"count": 0}

    @tool
    async def marker() -> str:
        """Records that it executed."""
        ran["count"] += 1
        return "did-run"

    tools = ToolRegistry()
    tools.register(marker)
    provider = ScriptedProvider(
        [
            ToolUseTurn(tool_name="marker", tool_input={}),
            TextTurn(text="done"),
        ]
    )
    agent = _agent(provider, tools)
    session = await agent.session()

    it = session.run("go").__aiter__()

    # Pull exactly one event, then stop pulling and yield the loop generously.
    first = await it.__anext__()
    assert first.type in {"system", "user"}
    await asyncio.sleep(0.05)

    # The producer is parked at the first yield: the tool has NOT run despite the
    # event loop having had ample time. This is backpressure — no run-ahead.
    assert ran["count"] == 0

    # Resume consumption: the rest of the stream drains and the tool executes.
    rest = [first]
    async for event in it:
        rest.append(event)

    assert ran["count"] == 1
    assert any(e.type == "tool_call_end" and e.tool_name == "marker" for e in rest)
    assert rest[-1].type == "result"
