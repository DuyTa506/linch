from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest


class RecordingProvider:
    id = "fake"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def context_window(self, model: str) -> int:
        return 128_000

    async def stream(self, req):
        from linch.types import TextBlock, Usage

        self.calls.append(
            {
                "messages": [
                    {
                        "role": message.role,
                        "content": [
                            block.text if isinstance(block, TextBlock) else str(block)
                            for block in message.content
                        ],
                    }
                    for message in req.messages
                ],
            }
        )
        yield {"type": "message_start", "model": req.model}
        yield {"type": "text_delta", "text": "done"}
        yield {
            "type": "message_end",
            "stop_reason": "end_turn",
            "usage": Usage(),
            "provider_metadata": None,
        }


async def _seed_store():
    from linch.memory import InMemoryKeywordMemoryStore, MemoryItem

    store = InMemoryKeywordMemoryStore()
    await store.upsert(
        [
            MemoryItem(
                id="m1",
                content="Scheduler V2 runs read search tools in parallel.",
                metadata={"label": "Scheduler"},
                namespace="docs",
            ),
            MemoryItem(
                id="m2",
                content="ToolResult stores citations and provenance metadata.",
                metadata={"label": "Citations"},
                namespace="docs",
            ),
        ]
    )
    return store


@pytest.mark.asyncio
async def test_keyword_memory_search_and_replace() -> None:
    from linch.memory import MemoryItem

    store = await _seed_store()

    hits = await store.search("parallel search", namespace="docs")
    assert [hit.item.id for hit in hits] == ["m1"]
    assert hits[0].score is not None
    assert hits[0].metadata["matched_terms"] == ["parallel", "search"]

    await store.upsert(
        [
            MemoryItem(
                id="m1",
                content="Scheduler V2 serializes writes.",
                namespace="docs",
            )
        ]
    )
    hits = await store.search("parallel search", namespace="docs")
    assert hits == []
    hits = await store.search("serializes writes", namespace="docs")
    assert [hit.item.id for hit in hits] == ["m1"]


@pytest.mark.asyncio
async def test_keyword_memory_store_work_does_not_block_event_loop(monkeypatch) -> None:
    from linch.memory import InMemoryKeywordMemoryStore, MemoryItem
    from linch.memory import keyword as keyword_mod

    original_tokenize = keyword_mod._tokenize

    def slow_tokenize(text: str) -> set[str]:
        time.sleep(0.02)
        return original_tokenize(text)

    monkeypatch.setattr(keyword_mod, "_tokenize", slow_tokenize)

    store = InMemoryKeywordMemoryStore()

    upsert_task = asyncio.create_task(
        store.upsert(
            [
                MemoryItem(id=f"m{i}", content="alpha beta", namespace="docs")
                for i in range(10)
            ]
        )
    )
    await asyncio.sleep(0.03)
    assert not upsert_task.done()
    await upsert_task

    store._token_cache.clear()
    search_task = asyncio.create_task(store.search("alpha", namespace="docs"))
    await asyncio.sleep(0.03)
    assert not search_task.done()
    hits = await search_task
    assert hits


@pytest.mark.asyncio
async def test_sqlite_memory_search_and_namespace(tmp_path) -> None:
    from linch.memory import MemoryItem, SqliteMemoryStore

    store = SqliteMemoryStore(tmp_path / "memory.sqlite")
    try:
        await store.upsert(
            [
                MemoryItem(id="a", content="alpha shared memory", namespace="left"),
                MemoryItem(id="b", content="alpha other memory", namespace="right"),
            ]
        )

        left = await store.search("alpha memory", namespace="left")
        right = await store.search("alpha memory", namespace="right")

        assert [hit.item.id for hit in left] == ["a"]
        assert [hit.item.id for hit in right] == ["b"]
    finally:
        store.close()


def test_postgres_memory_store_is_public_optional_export(monkeypatch) -> None:
    import sys

    from linch import PostgresMemoryStore as RootPostgresMemoryStore
    from linch.memory import PostgresMemoryStore

    assert RootPostgresMemoryStore is PostgresMemoryStore

    real_asyncpg = sys.modules.get("asyncpg")
    sys.modules["asyncpg"] = None  # type: ignore[assignment]
    try:
        with pytest.raises(ModuleNotFoundError, match="linch\\[postgres\\]"):
            PostgresMemoryStore("postgresql://user:pw@localhost/db")
    finally:
        if real_asyncpg is not None:
            sys.modules["asyncpg"] = real_asyncpg
        else:
            sys.modules.pop("asyncpg", None)


@pytest.mark.asyncio
async def test_memory_context_builder_injects_without_persisting() -> None:
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.memory import MemoryContextBuilder
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools

    store = await _seed_store()
    provider = RecordingProvider()
    agent = Agent(
        model="gpt-5",
        provider=provider,
        tools=empty_tools(),
        deps=store,
        context_builder=MemoryContextBuilder(namespace="docs", max_tokens=200),
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
    )
    session = await agent.session()

    events = []
    async for event in session.run("How do parallel search tools work?"):
        events.append(event)

    texts = [text for msg in provider.calls[0]["messages"] for text in msg["content"]]
    assert any("Retrieved memory:" in text for text in texts)
    assert any("Scheduler V2 runs read search tools in parallel." in text for text in texts)
    context_event = next(event for event in events if event.type == "context_build")
    assert context_event.metadata["memory"]["result_ids"] == ["m1"]

    persisted_texts = [
        block.text
        for message in session.provider_view
        for block in message.content
        if hasattr(block, "text")
    ]
    assert all("Retrieved memory:" not in text for text in persisted_texts)


@pytest.mark.asyncio
async def test_memory_context_builder_reports_budget_trimming() -> None:
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.memory import MemoryContextBuilder
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools

    store = await _seed_store()
    provider = RecordingProvider()
    agent = Agent(
        model="gpt-5",
        provider=provider,
        tools=empty_tools(),
        context_builder=MemoryContextBuilder(store, namespace="docs", max_tokens=1),
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
    )
    session = await agent.session()

    events = []
    async for event in session.run("parallel citations"):
        events.append(event)

    context_event = next(event for event in events if event.type == "context_build")
    assert context_event.budget["trimmed"] is True
    assert context_event.budget["used_tokens"] <= 1


@pytest.mark.asyncio
async def test_memory_search_and_upsert_tools_return_metadata() -> None:
    from linch.memory import MemorySearchTool, MemoryUpsertTool
    from linch.tools import ToolContext

    store = await _seed_store()
    ctx = ToolContext(cwd=".", session_id="s1", run_id="r1", session_store=None, deps=store)
    search_tool = MemorySearchTool(namespace="docs")
    upsert_tool = MemoryUpsertTool(namespace="docs")

    search_input = search_tool.validate({"query": "citations provenance", "limit": 5})
    result = await search_tool.execute(search_input, ctx)

    assert result.summary == "1 memory hit(s)"
    assert result.citations[0].id == "m2"
    assert result.metadata["result_ids"] == ["m2"]
    assert search_tool.resources(search_input)[0].resource == "memory:docs"
    assert search_tool.resources(search_input)[0].mode == "read"

    upsert_input = upsert_tool.validate({"id": "m3", "content": "new durable fact"})
    upsert_result = await upsert_tool.execute(upsert_input, ctx)
    assert upsert_result.summary == "memory upsert"
    assert upsert_tool.resources(upsert_input)[0].mode == "write"

    hits = await store.search("durable fact", namespace="docs")
    assert [hit.item.id for hit in hits] == ["m3"]
