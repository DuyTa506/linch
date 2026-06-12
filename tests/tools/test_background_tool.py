"""Background-any-tool (ROADMAP Phase 2.3).

Any tool call carrying a ``run_in_background`` hint is detached: it returns an
immediate ack as its tool result, runs as a detached task, and delivers its
completion as a ``<task-notification>`` drained on a later turn — the same
substrate the background-subagent path uses. Opt-in via
``Agent(enable_background_tools=True)``; off by default (byte-identical), where
the hint is just passed through to the tool.

linch imports happen inside the test bodies because sibling tests pop ``linch*``
modules from ``sys.modules``.
"""

from __future__ import annotations

import asyncio
from typing import Any


def _agent(provider: Any, tools: Any, **kwargs: Any) -> Any:
    from linch import Agent
    from linch.sessions import InMemorySessionStore

    return Agent(
        model="m",
        provider=provider,
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        cwd=".",
        tools=tools,
        **kwargs,
    )


def _texts(message: Any) -> str:
    return "".join(b.text for b in message.content if hasattr(b, "text"))


async def test_background_tool_returns_immediate_ack_then_notifies() -> None:
    from linch.evals import ScriptedProvider, TextTurn, ToolUseTurn
    from linch.tools import ToolRegistry, tool

    gate = asyncio.Event()
    started = asyncio.Event()

    @tool
    async def slow() -> str:
        """A slow tool that blocks until released."""
        started.set()
        await gate.wait()
        return "tool-finished"

    tools = ToolRegistry()
    tools.register(slow)
    provider = ScriptedProvider(
        [
            ToolUseTurn(tool_name="slow", tool_input={"run_in_background": True}),
            TextTurn(text="ack-seen"),
            TextTurn(text="all-done"),
        ]
    )
    agent = _agent(provider, tools, enable_background_tools=True)
    session = await agent.session()

    events1 = [event async for event in session.run("go")]

    # The detached tool starts (let the loop schedule it) but the turn did NOT
    # wait for it to finish — the gate is still closed.
    await asyncio.wait_for(started.wait(), timeout=1.0)
    assert not gate.is_set()
    # The tool result the model saw is an ack, not the tool's real output.
    end = [e for e in events1 if e.type == "tool_call_end" and e.tool_name == "slow"][0]
    assert "background" in end.result.lower()
    assert "tool-finished" not in end.result
    assert events1[-1].subtype == "success"

    # Release the tool and let the detached task finish.
    gate.set()
    await asyncio.gather(*session.background_tasks)

    # The completion arrives as a drained <task-notification> on the next turn.
    events2 = [event async for event in session.run("continue")]
    user_texts = " ".join(_texts(e.message) for e in events2 if e.type == "user")
    assert "task-notification" in user_texts
    assert "tool-finished" in user_texts


async def test_background_disabled_runs_inline() -> None:
    from linch.evals import ScriptedProvider, TextTurn, ToolUseTurn
    from linch.tools import ToolRegistry, tool

    ran = asyncio.Event()

    @tool
    async def quick(**kwargs: Any) -> str:
        """Runs inline; accepts extra kwargs so the passthrough hint is harmless."""
        ran.set()
        return "inline-result"

    tools = ToolRegistry()
    tools.register(quick)
    provider = ScriptedProvider(
        [
            ToolUseTurn(tool_name="quick", tool_input={"run_in_background": True}),
            TextTurn(text="done"),
        ]
    )
    # No enable_background_tools → byte-identical: the hint is passed through.
    agent = _agent(provider, tools)
    session = await agent.session()

    events = [event async for event in session.run("go")]

    assert ran.is_set()
    end = [e for e in events if e.type == "tool_call_end" and e.tool_name == "quick"][0]
    assert end.result == "inline-result"
    assert not getattr(session, "background_tasks", [])


async def test_abort_cancels_background_tool() -> None:
    from linch.evals import ScriptedProvider, TextTurn, ToolUseTurn
    from linch.tools import ToolRegistry, tool

    gate = asyncio.Event()
    started = asyncio.Event()

    @tool
    async def hang() -> str:
        """Blocks forever until cancelled."""
        started.set()
        await gate.wait()
        return "never"

    tools = ToolRegistry()
    tools.register(hang)
    provider = ScriptedProvider(
        [
            ToolUseTurn(tool_name="hang", tool_input={"run_in_background": True}),
            TextTurn(text="ack"),
        ]
    )
    agent = _agent(provider, tools, enable_background_tools=True)
    session = await agent.session()

    [event async for event in session.run("go")]
    await asyncio.wait_for(started.wait(), timeout=1.0)
    tasks = list(session.background_tasks)
    assert len(tasks) == 1

    session.abort()
    # Await the cancelled task settling, swallowing the CancelledError.
    try:
        await tasks[0]
    except asyncio.CancelledError:
        pass
    assert tasks[0].cancelled() or tasks[0].done()
