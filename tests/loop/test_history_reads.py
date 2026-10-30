from __future__ import annotations

from collections import Counter

import pytest
from test_run_resume import ScriptProvider, _registry


async def test_primary_turns_do_not_snapshot_audit_history(monkeypatch: pytest.MonkeyPatch) -> None:
    from linch import Agent, InMemoryRunStore
    from linch.events import ResultEvent
    from linch.session_log import SessionLog
    from linch.sessions import InMemorySessionStore

    provider = ScriptProvider(tool_names=["search"])
    agent = Agent(
        model="offline-model",
        provider=provider,
        tools=_registry({}, "search"),
        session_store=InMemorySessionStore(),
        run_store=InMemoryRunStore(),
        permissions={"mode": "skip-dangerous"},
        cwd=".",
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

    assert provider.calls == 2
    assert any(isinstance(event, ResultEvent) and event.subtype == "success" for event in events)
    assert reads["full_history"] == 0
    assert reads["provider_view"] == 4
