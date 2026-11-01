"""Test dependency values passed to tools from agent and run options."""

from __future__ import annotations

from typing import Any

import pytest

from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn, ToolUseTurn
from tests.support.tools import StubTool, tool_registry

_SENTINEL = object()
_OTHER = object()


@pytest.mark.parametrize(
    ("agent_deps", "run_deps", "expected"),
    [
        pytest.param(_SENTINEL, None, _SENTINEL, id="agent"),
        pytest.param(_SENTINEL, _OTHER, _OTHER, id="run-options"),
        pytest.param(None, None, None, id="unset"),
    ],
)
async def test_deps_are_threaded(agent_deps: Any, run_deps: Any, expected: Any) -> None:
    """ToolContext.deps uses run options when present, then the agent default."""

    received: list[Any] = []
    tool = StubTool(on_execute=lambda _input, ctx: received.append(ctx.deps))
    provider = ScriptedProvider([ToolUseTurn("Stub", {}), TextTurn("Done")])
    agent = make_agent(
        provider,
        model="gpt-5",
        tools=tool_registry(tool),
        run_store=None,
        deps=agent_deps,
    )
    session = await agent.session()

    if run_deps is None:
        async for _ in session.run("go"):
            pass
    else:
        from linch import RunOptions

        async for _ in session.run("go", RunOptions(deps=run_deps)):
            pass

    assert received == [expected]
