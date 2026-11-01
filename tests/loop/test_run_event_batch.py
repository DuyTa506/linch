from __future__ import annotations

from typing import Any

from linch.events import UserEvent
from linch.run_store import InMemoryRunStore
from linch.run_stores.postgres import PostgresRunStore
from linch.types import Message, TextBlock


def _events(*labels: str) -> list[UserEvent]:
    return [
        UserEvent(message=Message(role="user", content=[TextBlock(text=label)])) for label in labels
    ]


def _labels(rows: list) -> list[str]:
    return [row.event.message.content[0].text for row in rows]


async def _assert_contiguous(store: Any) -> None:
    run = await store.create_run("s1")
    assert await store.append_events(run.id, _events("a", "b", "c")) == [1, 2, 3]
    assert await store.append_events(run.id, _events("d", "e")) == [4, 5]
    rows = await store.load_events(run.id)
    assert [row.seq for row in rows] == [1, 2, 3, 4, 5]
    assert _labels(rows) == ["a", "b", "c", "d", "e"]


async def test_inmemory_append_events_returns_contiguous_seqs() -> None:
    await _assert_contiguous(InMemoryRunStore())


async def test_append_events_empty_is_noop() -> None:
    store = InMemoryRunStore()
    run = await store.create_run("s1")
    assert await store.append_events(run.id, []) == []


async def test_inmemory_append_events_honors_append_event_override() -> None:
    class _DropMarkedAppend(InMemoryRunStore):
        async def append_event(self, run_id: str, event) -> int:  # type: ignore[override]
            if event.message.content[0].text == "drop":
                return 0
            return await super().append_event(run_id, event)

    store = _DropMarkedAppend()
    run = await store.create_run("s1")
    seqs = await store.append_events(run.id, _events("keep", "drop", "keep2"))
    assert seqs == [1, 0, 2]
    assert _labels(await store.load_events(run.id)) == ["keep", "keep2"]


async def test_postgres_append_events_atomic_and_contiguous(
    pg_dsn,
) -> None:
    store = PostgresRunStore(pg_dsn)
    try:
        await _assert_contiguous(store)
    finally:
        await store.close()
