"""Optional batch-append capability on run stores (WS2).

`append_events` amortizes the per-event SELECT-MAX + INSERT + COMMIT into one
atomic batch, returning the 1-based seqs it assigned. `RunStore` itself is
unchanged; the method is an optional duck-typed capability.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from linch.events import UserEvent
from linch.run_store import InMemoryRunStore, SqliteRunStore
from linch.types import Message, TextBlock


def _events(*labels: str) -> list[UserEvent]:
    return [
        UserEvent(message=Message(role="user", content=[TextBlock(text=label)])) for label in labels
    ]


def _labels(rows: list) -> list[str]:
    return [row.event.message.content[0].text for row in rows]


async def test_inmemory_append_events_returns_contiguous_seqs() -> None:
    store = InMemoryRunStore()
    run = await store.create_run("s1")
    seqs = await store.append_events(run.id, _events("a", "b", "c"))
    assert seqs == [1, 2, 3]
    # A following batch continues the sequence.
    more = await store.append_events(run.id, _events("d", "e"))
    assert more == [4, 5]
    stored = await store.load_events(run.id)
    assert [row.seq for row in stored] == [1, 2, 3, 4, 5]


async def test_append_events_empty_is_noop() -> None:
    store = InMemoryRunStore()
    run = await store.create_run("s1")
    assert await store.append_events(run.id, []) == []


async def test_inmemory_append_events_honors_append_event_override() -> None:
    # Recovery tests subclass InMemoryRunStore and override append_event to drop a
    # start; the batch path must route through the override, not bypass it.
    class _DropMarkedAppend(InMemoryRunStore):
        async def append_event(self, run_id: str, event) -> int:  # type: ignore[override]
            if event.message.content[0].text == "drop":
                return 0
            return await super().append_event(run_id, event)

    store = _DropMarkedAppend()
    run = await store.create_run("s1")
    seqs = await store.append_events(run.id, _events("keep", "drop", "keep2"))
    assert seqs == [1, 0, 2]  # override honored: the dropped event returns 0
    assert _labels(await store.load_events(run.id)) == ["keep", "keep2"]


async def test_sqlite_append_events_atomic_and_contiguous(tmp_path: Path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    try:
        run = await store.create_run("s1")
        seqs = await store.append_events(run.id, _events("a", "b", "c"))
        assert seqs == [1, 2, 3]
        more = await store.append_events(run.id, _events("d", "e"))
        assert more == [4, 5]
        rows = await store.load_events(run.id)
        assert [row.seq for row in rows] == [1, 2, 3, 4, 5]
        assert _labels(rows) == ["a", "b", "c", "d", "e"]
    finally:
        await store.close()


async def test_sqlite_append_events_single_transaction(tmp_path: Path) -> None:
    # One MAX lookup + executemany + one commit: after the batch the on-disk rows
    # are exactly the batch with a contiguous (run_id, seq) primary key.
    store = SqliteRunStore(tmp_path / "runs.db")
    try:
        run = await store.create_run("s1")
        await store.append_events(run.id, _events("x", "y"))
    finally:
        await store.close()
    conn = sqlite3.connect(tmp_path / "runs.db")
    try:
        rows = conn.execute(
            "select seq from run_events where run_id = ? order by seq", (run.id,)
        ).fetchall()
        assert [r[0] for r in rows] == [1, 2]
    finally:
        conn.close()
