"""Deterministic tests for Postgres allocation SQL (no live DB required)."""

from __future__ import annotations

from typing import Any

import pytest

from linch.sessions.postgres import (
    PostgresSessionStore,
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
        if "UPDATE sessions" in normalized:
            if not self.session_exists:
                return {"session_id": None, "allocated_seqs": []}
            encoded = args[2]
            assert isinstance(encoded, list)
            first = self.current_seq + 1
            return {
                "session_id": args[0],
                "allocated_seqs": list(range(first, first + len(encoded))),
            }
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


async def test_append_rejects_unknown_session_before_inserting() -> None:
    conn = _FakeConnection(session_exists=False)
    store = _store_with(conn)

    with pytest.raises(KeyError, match="session not found: missing"):
        await store.append_messages(
            "missing",
            [Message(role="user", content=[TextBlock(text="orphan")])],
        )

    assert conn.executions == []
