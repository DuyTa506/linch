from __future__ import annotations

import asyncio

from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn, ToolUseTurn
from tests.support.tools import StubTool, tool_registry


async def test_slow_consumer_halts_the_producer() -> None:
    ran = {"count": 0}

    def record(_: dict[str, object], __: object) -> None:
        ran["count"] += 1

    tools = tool_registry(StubTool("marker", on_execute=record))
    provider = ScriptedProvider(
        [
            ToolUseTurn(tool_name="marker", tool_input={}),
            TextTurn(text="done"),
        ]
    )
    agent = make_agent(provider, tools=tools, model="m", run_store=None)
    session = await agent.session()

    it = session.run("go").__aiter__()

    first = await it.__anext__()
    assert first.type in {"system", "user"}
    await asyncio.sleep(0.05)

    assert ran["count"] == 0

    rest = [first]
    async for event in it:
        rest.append(event)

    assert ran["count"] == 1
    assert any(e.type == "tool_call_end" and e.tool_name == "marker" for e in rest)
    assert rest[-1].type == "result"
