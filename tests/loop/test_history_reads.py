from __future__ import annotations

from collections import Counter

import pytest

from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn, ToolUseTurn
from tests.support.stores import memory_run_store, memory_session_store
from tests.support.tools import StubTool, tool_registry


async def test_primary_turns_do_not_snapshot_audit_history(monkeypatch: pytest.MonkeyPatch) -> None:
    from linch.events import ResultEvent
    from linch.session_log import SessionLog

    provider = ScriptedProvider([ToolUseTurn("search", {}), TextTurn("done")])
    agent = make_agent(
        provider,
        model="offline-model",
        tools=tool_registry(StubTool("search")),
        session_store=memory_session_store(),
        run_store=memory_run_store(),
    )
    session = await agent.session()
    reads: Counter[str] = Counter()

    def counted(name: str) -> property:
        original = getattr(SessionLog, name).fget

        def read(log: SessionLog):
            reads[name] += 1
            return original(log)

        return property(read)

    for name in ("full_history", "provider_view"):
        monkeypatch.setattr(SessionLog, name, counted(name))

    events = [event async for event in session.run("hello")]

    assert len(provider.requests) == 2
    assert any(isinstance(event, ResultEvent) and event.subtype == "success" for event in events)
    assert reads["full_history"] == 0
    assert reads["provider_view"] == 4
