from __future__ import annotations

from collections.abc import AsyncIterator
from types import MappingProxyType
from typing import Any

import pytest


def _provider() -> Any:
    from linch.providers import BaseProvider

    class _Provider(BaseProvider):
        id = "fork-test"

        def context_window(self, model: str) -> int:
            return 100_000

        async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
            if False:  # pragma: no cover - the fork tests never call the provider
                yield {}

    return _Provider()


def _history() -> list[Any]:
    from linch.types import Message, TextBlock, ToolResultBlock, ToolUseBlock

    return [
        Message(role="user", content=[TextBlock(text="first request")]),
        Message(
            role="assistant",
            content=[ToolUseBlock(id="skill-1", name="Skill", input={"skill": "review"})],
        ),
        Message(
            role="user",
            content=[ToolResultBlock(tool_use_id="skill-1", content="review instructions")],
        ),
        Message(role="assistant", content=[TextBlock(text="first answer")]),
        Message(role="user", content=[TextBlock(text="second request")]),
        Message(
            role="assistant",
            content=[ToolUseBlock(id="skill-2", name="Skill", input={"skill": "/redteam"})],
        ),
        Message(
            role="user",
            content=[ToolResultBlock(tool_use_id="skill-2", content="red-team instructions")],
        ),
        Message(role="assistant", content=[TextBlock(text="second answer")]),
    ]


def _skills() -> list[dict[str, object]]:
    return [
        {"name": "review", "substituted_body": "review instructions", "invoked_at": 1.0},
        {"name": "redteam", "substituted_body": "red-team instructions", "invoked_at": 2.0},
    ]


async def _seed(agent: Any, store: Any) -> Any:
    from linch.sessions.tasks import CreateTaskInput

    await store.create(id="source", meta={"title": "source", "nested": {"value": 1}})
    await store.append_messages("source", _history())
    await store.set_invoked_skills("source", _skills())
    await store.create_task(
        "source", CreateTaskInput(subject="private task", description="source-only state")
    )
    return await agent.session(id="source")


async def test_memory_tail_fork_is_independent_and_copies_no_transient_state() -> None:
    from linch import Agent
    from linch.sessions import InMemorySessionStore, ProviderViewSnapshot
    from linch.sessions.fork import fork_session
    from linch.types import Message, TextBlock, message_to_dict

    store = InMemorySessionStore()
    agent = Agent(model="test", provider=_provider(), session_store=store, cwd=".")
    source = await _seed(agent, store)
    await store.save_provider_snapshot(
        source.id,
        ProviderViewSnapshot(
            provider_view=[Message(role="user", content=[TextBlock(text="compacted")])],
            covers_seq=4,
        ),
    )
    source.current_turn_permission_decisions["call"] = {"decision": "allow"}
    source.pending_notifications.append(Message(role="user", content=[TextBlock(text="pending")]))
    source.workers["worker"] = object()
    source_before = [message_to_dict(message) for message in source.full_history]

    child = await fork_session(
        agent,
        source,
        id="tail-fork",
        meta=MappingProxyType({"title": "child", "forked_at_seq": -1}),
    )

    assert [message_to_dict(message) for message in child.full_history] == source_before
    assert [record.name for record in child.invoked_skills] == ["review", "redteam"]
    assert child.meta == {
        "title": "child",
        "nested": {"value": 1},
        "forked_from_session_id": "source",
        "forked_at_seq": 8,
    }
    assert child._active is False
    assert child.active_run_id is None
    assert child.current_turn_permission_decisions == {}
    assert child.pending_notifications == []
    assert child.workers == {}
    assert await store.list_tasks(child.id) == []
    assert await store.load_provider_snapshot(child.id) is None

    # The memory store must not share mutable Message/Block or metadata values.
    child_block = child.full_history[0].content[0]
    assert isinstance(child_block, TextBlock)
    child_block.text = "changed only on child"
    child.meta["nested"] = {"value": 2}
    source_rows = await store.load_messages(source.id)
    source_record = await store.load(source.id)
    assert message_to_dict(source_rows[0].message) == source_before[0]
    assert source_record is not None and source_record.meta["nested"] == {"value": 1}
    assert [message_to_dict(message) for message in source.full_history] == source_before

    await agent.close()


async def test_earlier_fork_filters_skills_and_rejects_dangling_tool_use() -> None:
    from linch import Agent
    from linch.errors import ConfigError
    from linch.sessions import InMemorySessionStore
    from linch.sessions.fork import fork_session

    store = InMemorySessionStore()
    agent = Agent(model="test", provider=_provider(), session_store=store, cwd=".")
    source = await _seed(agent, store)

    child = await fork_session(agent, "source", before_seq=5, id="early-fork")
    assert len(child.full_history) == 4
    assert [record.name for record in child.invoked_skills] == ["review"]
    assert child.meta["forked_at_seq"] == 4

    # Excluding the tool-result message would leave seq 2's assistant tool use
    # unanswered, so the destination is never published.
    with pytest.raises(ConfigError, match="unanswered"):
        await fork_session(agent, source, before_seq=3, id="unsafe-fork")
    assert await store.load("unsafe-fork") is None

    source._active = True
    with pytest.raises(ConfigError, match="active source"):
        await fork_session(agent, source, id="moving-tail")
    explicit = await fork_session(agent, source, before_seq=5, id="stable-prefix")
    assert len(explicit.full_history) == 4
    source._active = False

    await agent.close()


async def test_custom_id_requires_positive_atomic_ownership() -> None:
    from linch import Agent
    from linch.errors import ConfigError
    from linch.sessions import InMemorySessionStore
    from linch.sessions.fork import fork_session
    from linch.types import Message

    class RaceLosingStore(InMemorySessionStore):
        def __init__(self) -> None:
            super().__init__()
            self.appended_to_target = False
            self.deleted_target = False

        async def create_if_absent(self, *, id: str, meta: dict[str, object] | None = None) -> Any:
            # Simulate another actor winning the destination-id race.
            await self.create(id=id, meta={"owner": "other"})
            return None

        async def append_messages(self, id: str, messages: list[Message]) -> Any:
            if id == "raced-target":
                self.appended_to_target = True
            return await super().append_messages(id, messages)

        async def delete(self, id: str) -> None:
            if id == "raced-target":
                self.deleted_target = True
            await super().delete(id)

    store = RaceLosingStore()
    agent = Agent(model="test", provider=_provider(), session_store=store, cwd=".")
    source = await _seed(agent, store)

    with pytest.raises(ConfigError, match="already exists"):
        await fork_session(agent, source, id="raced-target")

    raced = await store.load("raced-target")
    assert raced is not None and raced.meta == {"owner": "other"}
    assert store.appended_to_target is False
    assert store.deleted_target is False
    assert await store.load_messages("raced-target") == []

    # Checking the passed object closes a stale-registration bypass.
    agent._sessions.pop(source.id)
    source._active = True
    with pytest.raises(ConfigError, match="active source"):
        await fork_session(agent, source)
    source._active = False

    await agent.close()


async def test_fork_handoff_does_not_evict_or_delete_concurrent_active_session() -> None:
    import asyncio

    from linch import Agent
    from linch.sessions import InMemorySessionStore
    from linch.sessions.fork import fork_session

    class HandoffStore(InMemorySessionStore):
        def __init__(self) -> None:
            super().__init__()
            self.target_ready = asyncio.Event()
            self.finish_handoff = asyncio.Event()
            self.deleted_target = False

        async def set_invoked_skills(self, id: str, skills: list[dict[str, object]]) -> None:
            await super().set_invoked_skills(id, skills)
            if id == "handoff-target":
                self.target_ready.set()
                await self.finish_handoff.wait()

        async def delete(self, id: str) -> None:
            if id == "handoff-target":
                self.deleted_target = True
            await super().delete(id)

    store = HandoffStore()
    agent = Agent(model="test", provider=_provider(), session_store=store, cwd=".")
    source = await _seed(agent, store)
    forking = asyncio.create_task(fork_session(agent, source, id="handoff-target"))
    await store.target_ready.wait()

    concurrent = await agent.session(id="handoff-target")
    concurrent._active = True
    store.finish_handoff.set()

    with pytest.raises(RuntimeError, match="unexpectedly became active"):
        await forking

    assert agent._sessions["handoff-target"] is concurrent
    assert await store.load("handoff-target") is not None
    assert store.deleted_target is False

    concurrent._active = False
    await agent.close()


async def test_failed_partial_fork_cleanup_is_logged(caplog: Any) -> None:
    import logging

    from linch import Agent
    from linch.sessions import InMemorySessionStore
    from linch.sessions.fork import fork_session
    from linch.types import Message

    class CleanupFailingStore(InMemorySessionStore):
        async def append_messages(self, id: str, messages: list[Message]) -> Any:
            if id == "cleanup-target":
                raise RuntimeError("copy failed")
            return await super().append_messages(id, messages)

        async def delete(self, id: str) -> None:
            if id == "cleanup-target":
                raise RuntimeError("cleanup failed")
            await super().delete(id)

    store = CleanupFailingStore()
    agent = Agent(model="test", provider=_provider(), session_store=store, cwd=".")
    source = await _seed(agent, store)

    with caplog.at_level(logging.WARNING, logger="linch.sessions.fork"):
        with pytest.raises(RuntimeError, match="copy failed"):
            await fork_session(agent, source, id="cleanup-target")

    assert "failed to clean up partially forked session cleanup-target" in caplog.text
    await agent.close()


async def test_sqlite_fork_survives_restart_without_snapshot_or_tasks(tmp_path: Any) -> None:
    from linch import Agent
    from linch.sessions import ProviderViewSnapshot, SqliteSessionStore
    from linch.sessions.fork import fork_session
    from linch.types import Message, TextBlock, message_to_dict

    path = tmp_path / "sessions.db"
    first_store = SqliteSessionStore(path)
    first_agent = Agent(model="test", provider=_provider(), session_store=first_store, cwd=".")
    source = await _seed(first_agent, first_store)
    await first_store.save_provider_snapshot(
        source.id,
        ProviderViewSnapshot(
            provider_view=[Message(role="assistant", content=[TextBlock(text="summary")])],
            covers_seq=4,
        ),
    )
    source_before = [
        message_to_dict(row.message) for row in await first_store.load_messages("source")
    ]

    forked = await fork_session(first_agent, source, before_seq=5, id="sqlite-fork")
    assert [record.name for record in forked.invoked_skills] == ["review"]
    await first_agent.close()

    second_store = SqliteSessionStore(path)
    second_agent = Agent(model="test", provider=_provider(), session_store=second_store, cwd=".")
    reopened = await second_agent.session(id="sqlite-fork")

    assert len(reopened.full_history) == 4
    assert [record.name for record in reopened.invoked_skills] == ["review"]
    assert reopened.meta["forked_from_session_id"] == "source"
    assert reopened.meta["forked_at_seq"] == 4
    assert await second_store.list_tasks(reopened.id) == []
    assert await second_store.load_provider_snapshot(reopened.id) is None
    assert [
        message_to_dict(row.message) for row in await second_store.load_messages("source")
    ] == source_before

    await second_agent.close()
