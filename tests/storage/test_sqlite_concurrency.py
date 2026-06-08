"""Concurrency and non-blocking tests for the SQLite-backed stores.

These tests verify three things:

1. **Concurrent writers** — many coroutines hitting one store simultaneously
   produce no corruption and no OperationalError.
2. **Multi-subagent sharing** — parent + N child agent sessions sharing the
   same SqliteSessionStore see no interleaving or lost writes.
3. **Event-loop not blocked** — a heartbeat task shows the loop remains
   responsive while store writes run.
4. **close() back-compat** — both sync and async close paths work.

asyncio_mode=auto is configured project-wide (pyproject.toml), so no
pytest.mark.asyncio decorators are needed.
"""

from __future__ import annotations

import asyncio
import time

import pytest

# ── 1. Concurrent writers ────────────────────────────────────────────────────


async def test_session_store_concurrent_appends(tmp_path) -> None:
    """50 concurrent append_messages calls → unique, contiguous seq values."""
    from linch.sessions import SqliteSessionStore
    from linch.types import Message, TextBlock

    store = SqliteSessionStore(tmp_path / "sessions.db")
    rec = await store.create()

    msg = Message(role="user", content=[TextBlock(text="hi")])
    await asyncio.gather(*[store.append_messages(rec.id, [msg]) for _ in range(50)])

    rows = await store.load_messages(rec.id)
    assert len(rows) == 50
    seqs = [r.seq for r in rows]
    assert seqs == list(range(1, 51)), "seq values must be contiguous 1..50 with no gaps"
    await store.close()


async def test_memory_store_concurrent_upserts(tmp_path) -> None:
    """50 concurrent upserts to different IDs → all 50 items persisted."""
    from linch.memory.sqlite import SqliteMemoryStore
    from linch.memory.types import MemoryItem

    async with SqliteMemoryStore(tmp_path / "mem.db") as store:
        items = [
            MemoryItem(id=f"item-{i}", content=f"content {i}", namespace="test") for i in range(50)
        ]
        await asyncio.gather(*[store.upsert([item]) for item in items])

        results = await store.search("content", namespace="test", limit=100)
        assert len(results) == 50


async def test_filesystem_backend_concurrent_writes(tmp_path) -> None:
    """50 concurrent write() calls → all 50 paths exist."""
    from linch.filesystem.sqlite import SqliteFileBackend

    async with SqliteFileBackend(tmp_path / "fs.db") as fb:
        paths = [f"/file-{i}.txt" for i in range(50)]
        await asyncio.gather(*[fb.write(path, f"content {i}") for i, path in enumerate(paths)])

        all_paths = await fb.ls()
        assert len(all_paths) == 50
        assert set(all_paths) == set(paths)


# ── 2. Multi-subagent shared store ───────────────────────────────────────────


async def test_multiple_sessions_concurrent_writes(tmp_path) -> None:
    """10 sessions writing messages concurrently share one store without loss."""
    from linch.sessions import SqliteSessionStore
    from linch.types import Message, TextBlock

    store = SqliteSessionStore(tmp_path / "sessions.db")
    msg = Message(role="user", content=[TextBlock(text="tick")])

    # Create 10 sessions in parallel
    records = await asyncio.gather(*[store.create() for _ in range(10)])

    # Each session appends 5 messages concurrently
    async def write_session(sid: str) -> None:
        for _ in range(5):
            await store.append_messages(sid, [msg])

    await asyncio.gather(*[write_session(rec.id) for rec in records])

    # Every session should have exactly 5 messages
    for rec in records:
        rows = await store.load_messages(rec.id)
        assert len(rows) == 5, f"session {rec.id} expected 5 messages, got {len(rows)}"

    await store.close()


# ── 3. Event-loop not blocked ────────────────────────────────────────────────


async def test_store_writes_do_not_block_event_loop(tmp_path) -> None:
    """The event loop stays responsive while SQLite writes are in flight.

    A heartbeat coroutine records wall-clock gaps between ticks.  On the old
    blocking code the event loop froze during commit(); on the executor version
    the heartbeat keeps firing between writes.

    Threshold is deliberately generous (500 ms) to avoid CI flakiness.
    """
    from linch.sessions import SqliteSessionStore
    from linch.types import Message, TextBlock

    store = SqliteSessionStore(tmp_path / "sessions.db")
    rec = await store.create()
    msg = Message(role="user", content=[TextBlock(text="x")])

    gaps: list[float] = []
    stop_event = asyncio.Event()

    async def heartbeat() -> None:
        last = time.monotonic()
        while not stop_event.is_set():
            await asyncio.sleep(0.02)  # 20 ms tick
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    hb_task = asyncio.ensure_future(heartbeat())

    # Issue 20 writes back-to-back; each commits to disk
    for _ in range(20):
        await store.append_messages(rec.id, [msg])

    stop_event.set()
    await hb_task

    max_gap = max(gaps) if gaps else 0.0
    assert max_gap < 0.5, (
        f"Event loop was blocked for {max_gap:.3f}s during SQLite writes — "
        "store writes must run off the event loop (SqliteExecutor)."
    )
    await store.close()


# ── 4. close() back-compat ───────────────────────────────────────────────────


async def test_session_store_async_close(tmp_path) -> None:
    """SqliteSessionStore.close() is awaitable and idempotent."""
    from linch.sessions import SqliteSessionStore

    store = SqliteSessionStore(tmp_path / "sessions.db")
    await store.create()
    await store.close()
    await store.close()  # second close must not raise


async def test_memory_store_sync_close(tmp_path) -> None:
    """SqliteMemoryStore supports both sync and async context managers."""
    from linch.memory.sqlite import SqliteMemoryStore
    from linch.memory.types import MemoryItem

    # sync with
    with SqliteMemoryStore(tmp_path / "mem.db") as store:
        await store.upsert([MemoryItem(id="x", content="hello", namespace="test")])

    # async with
    async with SqliteMemoryStore(tmp_path / "mem2.db") as store:
        await store.upsert([MemoryItem(id="y", content="world", namespace="test")])


async def test_filesystem_backend_sync_close(tmp_path) -> None:
    """SqliteFileBackend supports both sync and async context managers."""
    from linch.filesystem.sqlite import SqliteFileBackend

    # sync with
    with SqliteFileBackend(tmp_path / "fs.db") as fb:
        await fb.write("/a.txt", "hello")
        assert await fb.read("/a.txt") == "hello"

    # async with
    async with SqliteFileBackend(tmp_path / "fs2.db") as fb:
        await fb.write("/b.txt", "world")
        assert await fb.read("/b.txt") == "world"


# ── 5. Executor surfaces connect errors ─────────────────────────────────────


async def test_executor_surfaces_bad_path_error() -> None:
    """run() raises if the SQLite path is unwritable (e.g. a directory)."""
    from linch.storage._executor import SqliteExecutor

    with pytest.raises((OSError, RuntimeError, Exception)):  # noqa: B017
        exec_ = SqliteExecutor("/", init=lambda _: None)  # root dir, not writable
        await exec_.run(lambda conn: conn.execute("select 1").fetchone())
        await exec_.close()
