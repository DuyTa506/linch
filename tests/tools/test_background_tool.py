"""Background-any-tool.

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

from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn, ToolUseTurn
from tests.support.tools import tool_registry


def _texts(message: Any) -> str:
    return "".join(b.text for b in message.content if hasattr(b, "text"))


async def test_background_tool_returns_immediate_ack_then_notifies() -> None:
    from linch.tools import tool

    gate = asyncio.Event()
    started = asyncio.Event()

    @tool
    async def slow() -> str:
        """A slow tool that blocks until released."""
        started.set()
        await gate.wait()
        return "tool-finished"

    tools = tool_registry(slow)
    provider = ScriptedProvider(
        [
            ToolUseTurn(tool_name="slow", tool_input={"run_in_background": True}),
            TextTurn(text="ack-seen"),
            TextTurn(text="all-done"),
        ]
    )
    agent = make_agent(
        provider, model="m", tools=tools, run_store=None, enable_background_tools=True
    )
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
    from linch.tools import tool

    ran = asyncio.Event()

    @tool
    async def quick(**kwargs: Any) -> str:
        """Runs inline; accepts extra kwargs so the passthrough hint is harmless."""
        ran.set()
        return "inline-result"

    tools = tool_registry(quick)
    provider = ScriptedProvider(
        [
            ToolUseTurn(tool_name="quick", tool_input={"run_in_background": True}),
            TextTurn(text="done"),
        ]
    )
    # No enable_background_tools → byte-identical: the hint is passed through.
    agent = make_agent(provider, model="m", tools=tools, run_store=None)
    session = await agent.session()

    events = [event async for event in session.run("go")]

    assert ran.is_set()
    end = [e for e in events if e.type == "tool_call_end" and e.tool_name == "quick"][0]
    assert end.result == "inline-result"
    assert not getattr(session, "background_tasks", [])


async def test_abort_cancels_background_tool() -> None:
    from linch.tools import tool

    gate = asyncio.Event()
    started = asyncio.Event()

    @tool
    async def hang() -> str:
        """Blocks forever until cancelled."""
        started.set()
        await gate.wait()
        return "never"

    tools = tool_registry(hang)
    provider = ScriptedProvider(
        [
            ToolUseTurn(tool_name="hang", tool_input={"run_in_background": True}),
            TextTurn(text="ack"),
        ]
    )
    agent = make_agent(
        provider, model="m", tools=tools, run_store=None, enable_background_tools=True
    )
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


async def test_background_tool_completion_runs_post_tool_use_hooks() -> None:
    from linch import RedactionConfig, RedactionHook, RedactionRule
    from linch.abort import AbortContext
    from linch.scheduler import execute_tool_calls
    from linch.tools import ToolRegistry, tool
    from linch.types import ToolUseBlock
    from tests.support.scheduler import scheduler_agent, scheduler_session

    @tool
    async def secret() -> str:
        """Return a secret for the redaction regression."""
        return "SECRET_VALUE"

    registry = ToolRegistry()
    registry.register(secret)
    hook = RedactionHook(RedactionConfig(rules=(RedactionRule(r"SECRET_VALUE", "[REDACTED]"),)))
    agent = scheduler_agent(
        registry,
        hooks=[hook],
        enable_background_tools=True,
    )
    session = scheduler_session()

    events = [
        event
        async for event in execute_tool_calls(
            [
                ToolUseBlock(
                    id="call",
                    name="secret",
                    input={"run_in_background": True},
                )
            ],
            agent,
            session,
            AbortContext(),
        )
    ]
    assert events[-1].type == "tool_call_end"
    await asyncio.gather(*session.background_tasks)

    notification = session.pending_notifications[0]
    text = notification.content[0].text
    assert "[REDACTED]" in text
    assert "SECRET_VALUE" not in text


async def test_background_post_tool_use_keeps_origin_run_id_after_run_settles() -> None:
    from linch.abort import AbortContext
    from linch.scheduler import execute_tool_calls
    from linch.tools import ToolRegistry, tool
    from linch.types import ToolUseBlock
    from tests.support.scheduler import scheduler_agent, scheduler_session

    seen_run_ids: list[str] = []

    @tool
    async def quick() -> str:
        """Return a completed background result."""
        return "done"

    class CaptureRunId:
        def on_post_tool_use(self, ctx: Any) -> None:
            seen_run_ids.append(ctx.run_id)

    registry = ToolRegistry()
    registry.register(quick)
    agent = scheduler_agent(
        registry,
        hooks=[CaptureRunId()],
        enable_background_tools=True,
    )
    session = scheduler_session(run_id="origin-run")

    [
        event
        async for event in execute_tool_calls(
            [ToolUseBlock(id="call", name="quick", input={"run_in_background": True})],
            agent,
            session,
            AbortContext(),
        )
    ]
    session.active_run_id = None
    await asyncio.gather(*session.background_tasks)

    assert seen_run_ids == ["origin-run"]


async def test_background_tool_completion_uses_captured_origin_lease() -> None:
    """A detached completion must not write with a lease adopted later."""
    from types import SimpleNamespace

    from linch.abort import AbortContext
    from linch.events import BackgroundWorkerEvent
    from linch.run_store import InMemoryRunStore
    from linch.scheduler import execute_tool_calls
    from linch.tools import ToolRegistry, tool
    from linch.types import ToolUseBlock
    from tests.support.scheduler import scheduler_agent, scheduler_session

    started = asyncio.Event()
    release = asyncio.Event()

    @tool
    async def slow() -> str:
        """Wait until the test releases the detached completion."""
        started.set()
        await release.wait()
        return "done"

    registry = ToolRegistry()
    registry.register(slow)
    store = InMemoryRunStore()
    await store.create_run("s1", id="origin-run")
    origin_lease = await store.claim("origin-run", "origin-worker", 30.0)
    agent = scheduler_agent(
        registry,
        run_store=store,
        enable_background_tools=True,
    )
    session = scheduler_session(
        run_id="origin-run",
        _event_journal=SimpleNamespace(lease=origin_lease),
    )

    [
        event
        async for event in execute_tool_calls(
            [ToolUseBlock(id="call", name="slow", input={"run_in_background": True})],
            agent,
            session,
            AbortContext(),
        )
    ]
    await asyncio.wait_for(started.wait(), timeout=1.0)

    await store.release(origin_lease)
    replacement_lease = await store.claim("origin-run", "later-worker", 30.0)
    session.active_run_id = "later-run"
    session._event_journal = SimpleNamespace(lease=replacement_lease)
    release.set()
    await asyncio.gather(*session.background_tasks)

    rows = await store.load_events("origin-run")
    assert not any(isinstance(row.event, BackgroundWorkerEvent) for row in rows)
