"""Postgres backend tests — skipped unless asyncpg + a live DB are available.

To run:
    pip install 'agent-kit[postgres]'
    AGENT_KIT_TEST_PG_DSN=postgresql://user:pw@localhost/agentkit_test \
        pytest tests/storage/test_postgres.py -v
"""

from __future__ import annotations

import os

import pytest

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg not installed")

DSN = os.environ.get("AGENT_KIT_TEST_PG_DSN", "")
needs_pg = pytest.mark.skipif(not DSN, reason="AGENT_KIT_TEST_PG_DSN not set")


# ── SessionStore ─────────────────────────────────────────────────────────────


@needs_pg
async def test_pg_session_store_round_trip() -> None:
    from agent_kit.sessions.postgres import PostgresSessionStore
    from agent_kit.types import Message, TextBlock

    store = PostgresSessionStore(DSN)
    try:
        rec = await store.create(meta={"title": "pg-test"})
        msg = Message(role="user", content=[TextBlock(text="hello postgres")])
        await store.append_messages(rec.id, [msg])

        rows = await store.load_messages(rec.id)
        assert len(rows) == 1
        assert rows[0].seq == 1
        assert rows[0].message.content[0].text == "hello postgres"  # type: ignore[union-attr]

        loaded = await store.load(rec.id)
        assert loaded is not None
        assert loaded.meta["title"] == "pg-test"

        await store.delete(rec.id)
        assert await store.load(rec.id) is None
    finally:
        await store.close()


@needs_pg
async def test_pg_session_store_concurrent_appends() -> None:
    """50 concurrent appends to one session → contiguous seq values."""
    import asyncio

    from agent_kit.sessions.postgres import PostgresSessionStore
    from agent_kit.types import Message, TextBlock

    store = PostgresSessionStore(DSN, min_size=5, max_size=20)
    try:
        rec = await store.create()
        msg = Message(role="user", content=[TextBlock(text="tick")])
        await asyncio.gather(*[store.append_messages(rec.id, [msg]) for _ in range(50)])

        rows = await store.load_messages(rec.id)
        assert len(rows) == 50
        seqs = [r.seq for r in rows]
        assert seqs == list(range(1, 51))
    finally:
        await store.delete(rec.id)
        await store.close()


# ── MemoryStore ───────────────────────────────────────────────────────────────


@needs_pg
async def test_pg_memory_store_round_trip() -> None:

    from agent_kit.memory.postgres import PostgresMemoryStore
    from agent_kit.memory.types import MemoryItem

    store = PostgresMemoryStore(DSN)
    try:
        items = [
            MemoryItem(id=f"pg-{i}", content=f"agent memory {i}", namespace="pg-test")
            for i in range(10)
        ]
        await store.upsert(items)

        results = await store.search("agent memory", namespace="pg-test", limit=20)
        assert len(results) == 10

        # Upsert update
        await store.upsert([
            MemoryItem(id="pg-0", content="updated content", namespace="pg-test")
        ])
        results2 = await store.search("updated", namespace="pg-test", limit=5)
        assert any(r.item.id == "pg-0" for r in results2)
    finally:
        await store.close()


# ── FileBackend ───────────────────────────────────────────────────────────────


@needs_pg
async def test_pg_file_backend_round_trip() -> None:
    from agent_kit.filesystem.postgres import PostgresFileBackend

    fb = PostgresFileBackend(DSN)
    try:
        await fb.write("/pg/test.txt", "hello postgres fs")
        assert await fb.read("/pg/test.txt") == "hello postgres fs"
        assert await fb.exists("/pg/test.txt")
        assert not await fb.exists("/pg/missing.txt")

        paths = await fb.ls("/pg")
        assert "/pg/test.txt" in paths

        n = await fb.edit("/pg/test.txt", "hello", "goodbye")
        assert n == 1
        assert await fb.read("/pg/test.txt") == "goodbye postgres fs"

        await fb.delete("/pg/test.txt")
        assert not await fb.exists("/pg/test.txt")
    finally:
        await fb.close()


@needs_pg
async def test_pg_file_backend_concurrent_writes() -> None:
    import asyncio

    from agent_kit.filesystem.postgres import PostgresFileBackend

    fb = PostgresFileBackend(DSN, min_size=5, max_size=20)
    try:
        paths = [f"/pg-concurrent/{i}.txt" for i in range(50)]
        await asyncio.gather(
            *[fb.write(p, f"content {i}") for i, p in enumerate(paths)]
        )
        all_paths = await fb.ls("/pg-concurrent")
        assert len(all_paths) == 50
    finally:
        for p in paths:
            await fb.delete(p)
        await fb.close()


# ── Import-guard tests (no live DB needed) ────────────────────────────────────


def test_pg_stores_fail_fast_without_asyncpg(monkeypatch) -> None:
    """Construction raises ModuleNotFoundError with the install hint."""
    import sys

    real_asyncpg = sys.modules.get("asyncpg")
    sys.modules["asyncpg"] = None  # type: ignore[assignment]
    try:
        from agent_kit.storage._pg import _import_asyncpg

        with pytest.raises(ModuleNotFoundError, match="agent-kit\\[postgres\\]"):
            _import_asyncpg()
    finally:
        if real_asyncpg is not None:
            sys.modules["asyncpg"] = real_asyncpg
        elif "asyncpg" in sys.modules:
            del sys.modules["asyncpg"]
