"""Deterministic tests for Postgres allocation SQL (no live DB required)."""

from __future__ import annotations

from typing import Any

import pytest

from linch.sessions.postgres import (
    PostgresSessionStore,
    _allocate_task_id_pg,
    _lock_session_pg,
)
from linch.types import Message, TextBlock


class _AsyncContext:
    def __init__(self, value: Any) -> None:
        self.value = value

    async def __aenter__(self) -> Any:
        return self.value

    async def __aexit__(self, *args: object) -> None:
        return None


class _FakeConnection:
    def __init__(
        self,
        *,
        session_exists: bool = True,
        current_seq: int = 0,
        insert_session_wins: bool = True,
    ) -> None:
        self.session_exists = session_exists
        self.current_seq = current_seq
        self.insert_session_wins = insert_session_wins
        self.queries: list[str] = []
        self.executions: list[tuple[str, tuple[object, ...]]] = []
        self.allocated_task_id = 7

    def transaction(self) -> _AsyncContext:
        return _AsyncContext(self)

    async def fetchval(self, query: str, *args: object) -> object:
        normalized = " ".join(query.split())
        self.queries.append(normalized)
        if "FROM sessions" in normalized:
            return args[0] if self.session_exists else None
        if "MAX(seq)" in normalized:
            return self.current_seq
        if "INSERT INTO task_counters" in normalized:
            return self.allocated_task_id
        raise AssertionError(f"unexpected fetchval: {normalized}")

    async def execute(self, query: str, *args: object) -> None:
        self.executions.append((" ".join(query.split()), args))

    async def fetchrow(self, query: str, *args: object) -> dict[str, object] | None:
        normalized = " ".join(query.split())
        self.queries.append(normalized)
        if "INSERT INTO sessions" not in normalized:
            raise AssertionError(f"unexpected fetchrow: {normalized}")
        if not self.insert_session_wins:
            return None
        return {
            "id": args[0],
            "created_at": args[1],
            "updated_at": args[2],
            "meta": args[3],
            "invoked_skills": args[4],
        }


class _FakePool:
    def __init__(self, conn: _FakeConnection) -> None:
        self.conn = conn

    def acquire(self) -> _AsyncContext:
        return _AsyncContext(self.conn)


def _store_with(conn: _FakeConnection) -> PostgresSessionStore:
    # Bypass __init__: these tests exercise allocation behavior, not the
    # optional asyncpg import guard.
    store = PostgresSessionStore.__new__(PostgresSessionStore)
    store._initialized = True
    store._pool = _FakePool(conn)
    return store


async def test_append_locks_session_before_reading_max_sequence() -> None:
    conn = _FakeConnection(current_seq=4)
    store = _store_with(conn)
    messages = [
        Message(role="user", content=[TextBlock(text="a")]),
        Message(role="assistant", content=[TextBlock(text="b")]),
    ]

    stored = await store.append_messages("s-1", messages)

    assert [item.seq for item in stored] == [5, 6]
    assert "FOR UPDATE" in conn.queries[0]
    assert "MAX(seq)" in conn.queries[1]
    inserts = [query for query, _ in conn.executions if "INSERT INTO messages" in query]
    assert len(inserts) == 2


async def test_append_rejects_unknown_session_before_inserting() -> None:
    conn = _FakeConnection(session_exists=False)
    store = _store_with(conn)

    with pytest.raises(KeyError, match="session not found: missing"):
        await store.append_messages(
            "missing",
            [Message(role="user", content=[TextBlock(text="orphan")])],
        )

    assert conn.executions == []


async def test_task_allocator_uses_atomic_incrementing_upsert() -> None:
    conn = _FakeConnection()

    allocated = await _allocate_task_id_pg(conn, "s-1")

    assert allocated == "7"
    sql = conn.queries[-1]
    assert "ON CONFLICT (session_id) DO UPDATE" in sql
    assert "task_counters.next_id + 1" in sql
    assert "RETURNING next_id - 1" in sql


async def test_session_lock_raises_for_missing_parent() -> None:
    conn = _FakeConnection(session_exists=False)

    with pytest.raises(KeyError, match="session not found: absent"):
        await _lock_session_pg(conn, "absent")


async def test_create_if_absent_proves_ownership_with_insert_returning() -> None:
    conn = _FakeConnection(insert_session_wins=True)
    store = _store_with(conn)

    created = await store.create_if_absent(id="fork-1", meta={"owner": "this-run"})

    assert created is not None
    assert created.id == "fork-1"
    assert created.meta == {"owner": "this-run"}
    sql = conn.queries[0]
    assert "INSERT INTO sessions" in sql
    assert "ON CONFLICT (id) DO NOTHING" in sql
    assert "RETURNING id, created_at, updated_at, meta, invoked_skills" in sql
    assert "SELECT" not in sql
    assert any("INSERT INTO task_counters" in query for query, _ in conn.executions)


async def test_create_if_absent_loser_does_not_initialize_existing_session() -> None:
    conn = _FakeConnection(insert_session_wins=False)
    store = _store_with(conn)

    created = await store.create_if_absent(id="taken", meta={"owner": "loser"})

    assert created is None
    assert conn.executions == []
