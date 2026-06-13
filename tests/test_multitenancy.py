"""Multi-tenancy & cancellation isolation (ROADMAP Phase 5.2).

N independent `Agent` instances must run concurrently in one host process with no
cross-talk: each agent owns its sessions, tool registry, and extension state, and
closing or aborting one agent never disturbs another. The audit found no
process-global mutable state; these tests pin that guarantee.

Verify: concurrent runs across agents stay isolated (each session sees only its
own turns); `agent.close()` drains *its* background workers without cancelling a
second agent's in-flight worker.
"""

from __future__ import annotations

import asyncio
from typing import Any


def _agent(provider: Any, tools: Any = None, **kwargs: Any) -> Any:
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


async def test_concurrent_agents_do_not_cross_talk() -> None:
    from linch.evals import ScriptedProvider, TextTurn

    # Three agents, each scripted to emit a distinct final answer.
    agents = []
    for tag in ("alpha", "beta", "gamma"):
        provider = ScriptedProvider([TextTurn(text=f"answer-{tag}")])
        agents.append((tag, _agent(provider)))

    async def _run(tag: str, agent: Any) -> str:
        session = await agent.session()
        finals = [
            _texts(e.message) async for e in session.run(f"prompt-{tag}") if e.type == "assistant"
        ]
        # Each agent must hold exactly its own one session — no leakage.
        assert list(agent._sessions) == [session.id]
        return finals[-1]

    results = await asyncio.gather(*(_run(tag, agent) for tag, agent in agents))

    assert results == ["answer-alpha", "answer-beta", "answer-gamma"]
    # Session ids are unique across agents — no shared/global session registry.
    ids = [next(iter(agent._sessions)) for _, agent in agents]
    assert len(set(ids)) == len(ids)


async def test_close_drains_own_workers_without_touching_other_agent() -> None:
    from linch.evals import ScriptedProvider, TextTurn, ToolUseTurn
    from linch.tools import ToolRegistry, tool

    def _hanging_agent() -> tuple[Any, asyncio.Event]:
        started = asyncio.Event()

        @tool
        async def hang() -> str:
            """Blocks forever until cancelled."""
            started.set()
            await asyncio.Event().wait()
            return "never"

        tools = ToolRegistry()
        tools.register(hang)
        provider = ScriptedProvider(
            [
                ToolUseTurn(tool_name="hang", tool_input={"run_in_background": True}),
                TextTurn(text="ack"),
            ]
        )
        return _agent(provider, tools, enable_background_tools=True), started

    agent_a, started_a = _hanging_agent()
    agent_b, started_b = _hanging_agent()
    session_a = await agent_a.session()
    session_b = await agent_b.session()

    [e async for e in session_a.run("go")]
    [e async for e in session_b.run("go")]
    await asyncio.wait_for(started_a.wait(), timeout=1.0)
    await asyncio.wait_for(started_b.wait(), timeout=1.0)

    task_a = list(session_a.background_tasks)[0]
    task_b = list(session_b.background_tasks)[0]

    # Close only agent A. Its worker drains; B's keeps running, untouched.
    await agent_a.close()
    try:
        await task_a
    except asyncio.CancelledError:
        pass

    assert task_a.cancelled() or task_a.done()
    assert not task_b.done()
    assert agent_a._sessions == {}

    # Cleanup: closing B drains its worker too.
    await agent_b.close()
    try:
        await task_b
    except asyncio.CancelledError:
        pass
    assert task_b.cancelled() or task_b.done()
