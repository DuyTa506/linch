from __future__ import annotations

from linch.run_store import InMemoryRunStore


async def test_append_events_empty_is_noop() -> None:
    store = InMemoryRunStore()
    run = await store.create_run("s1")
    assert await store.append_events(run.id, []) == []
