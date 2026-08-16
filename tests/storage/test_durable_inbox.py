from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator

import pytest

from linch.durability import DurabilityOptions, InboxDelivery, JsonValue
from linch.errors import ConfigError
from linch.sessions import InMemorySessionStore, SqliteSessionStore
from linch.sessions.postgres import _SCHEMA, PostgresSessionStore
from linch.types import Message, TextBlock


def _message(text: str) -> Message:
    return Message(role="user", content=[TextBlock(text=text)])


async def _stores(tmp_path) -> AsyncIterator[InMemorySessionStore | SqliteSessionStore]:
    memory = InMemorySessionStore()
    yield memory
    sqlite = SqliteSessionStore(tmp_path / "inbox.db")
    try:
        yield sqlite
    finally:
        await sqlite.close()


def test_durability_options_are_opt_in_and_strict_profile_is_stable() -> None:
    assert DurabilityOptions() == DurabilityOptions(
        durable_inbox=False,
        exact_model_input=False,
        delivery_lease_s=30.0,
    )
    assert DurabilityOptions.strict_v1() == DurabilityOptions(
        durable_inbox=True,
        exact_model_input=True,
        delivery_lease_s=30.0,
    )
    with pytest.raises(ConfigError, match="positive finite"):
        DurabilityOptions(delivery_lease_s=float("nan"))


def test_inbox_delivery_is_user_only_json_safe_and_detached() -> None:
    metadata: dict[str, JsonValue] = {"nested": [1, {"ok": True}]}
    delivery = InboxDelivery("one", _message("hello"), metadata=metadata)
    metadata["nested"].append(2)  # type: ignore[union-attr]

    assert delivery.metadata == {"nested": [1, {"ok": True}]}
    assert len(delivery.digest) == 64

    with pytest.raises(ConfigError, match="role='user'"):
        InboxDelivery("assistant", Message(role="assistant", content=[]))
    with pytest.raises(ConfigError, match="NaN or Infinity"):
        InboxDelivery("nan", _message("bad"), metadata={"value": float("nan")})
    with pytest.raises(ConfigError, match="string keys"):
        InboxDelivery("key", _message("bad"), metadata={1: "bad"})  # type: ignore[dict-item]
    with pytest.raises(ConfigError, match="string object keys"):
        InboxDelivery("nested-key", _message("bad"), metadata={"nested": {1: "bad"}})  # type: ignore[dict-item]
    with pytest.raises(ConfigError, match="strictly JSON-serializable"):
        InboxDelivery("tuple", _message("bad"), metadata={"tuple": (1, 2)})  # type: ignore[dict-item]


async def test_store_inbox_fifo_dedupe_conflict_and_response_loss(tmp_path) -> None:
    async for store in _stores(tmp_path):
        session = await store.create()
        await store.append_messages(session.id, [_message("existing")])
        first = InboxDelivery("delivery-1", _message("first"), metadata={"n": 1})
        second = InboxDelivery("delivery-2", _message("second"), source="mailbox")

        await store.enqueue_inbox(session.id, first)
        await store.enqueue_inbox(session.id, first)
        await store.enqueue_inbox(session.id, second)
        with pytest.raises(ConfigError, match="conflicting inbox delivery"):
            await store.enqueue_inbox(
                session.id,
                InboxDelivery("delivery-1", _message("changed"), metadata={"n": 1}),
            )

        committed = await store.commit_inbox(session.id, after_seq=1)
        assert [row.seq for row in committed] == [2, 3]
        assert [row.message.content[0].text for row in committed] == ["first", "second"]  # type: ignore[union-attr]

        # Simulate a commit whose successful response was lost.  The caller's
        # old cursor recovers all history after it, without another insertion.
        recovered = await store.commit_inbox(session.id, after_seq=1)
        assert [row.seq for row in recovered] == [2, 3]
        assert len(await store.load_messages(session.id)) == 3

        # Receipts remain authoritative after payload pruning.
        await store.enqueue_inbox(session.id, first)
        with pytest.raises(ConfigError, match="conflicting inbox delivery"):
            await store.enqueue_inbox(
                session.id,
                InboxDelivery("delivery-1", _message("post-commit conflict")),
            )


async def test_commit_returns_non_inbox_messages_newer_than_cursor(tmp_path) -> None:
    async for store in _stores(tmp_path):
        session = await store.create()
        await store.append_messages(session.id, [_message("direct")])

        rows = await store.commit_inbox(session.id, after_seq=0)

        assert [row.message.content[0].text for row in rows] == ["direct"]  # type: ignore[union-attr]


async def test_store_inbox_unknown_session_and_cursor_validation(tmp_path) -> None:
    async for store in _stores(tmp_path):
        with pytest.raises(KeyError):
            await store.enqueue_inbox("missing", InboxDelivery("one", _message("x")))
        with pytest.raises(KeyError):
            await store.commit_inbox("missing", after_seq=0)

        session = await store.create()
        with pytest.raises(ConfigError, match="non-negative integer"):
            await store.commit_inbox(session.id, after_seq=-1)
        with pytest.raises(ConfigError, match="non-negative integer"):
            await store.commit_inbox(session.id, after_seq=True)


async def test_sqlite_receipt_prunes_payload_and_survives_reopen(tmp_path) -> None:
    path = tmp_path / "reopen.db"
    store = SqliteSessionStore(path)
    session = await store.create()
    delivery = InboxDelivery("stable", _message("payload"), metadata={"large": "x" * 1000})
    await store.enqueue_inbox(session.id, delivery)
    await store.commit_inbox(session.id, after_seq=0)
    receipt = await store._exec.run(
        lambda conn: conn.execute(
            "select digest, message, source, metadata_json, message_seq "
            "from session_inbox where session_id = ? and delivery_id = ?",
            (session.id, delivery.delivery_id),
        ).fetchone()
    )
    assert tuple(receipt) == (delivery.digest, None, None, None, 1)
    await store.close()

    reopened = SqliteSessionStore(path)
    try:
        await reopened.enqueue_inbox(session.id, delivery)
        rows = await reopened.commit_inbox(session.id, after_seq=0)
        assert len(rows) == 1
        assert rows[0].seq == 1
    finally:
        await reopened.close()


async def test_sqlite_two_connections_enqueue_without_lost_fifo_rows(tmp_path) -> None:
    path = tmp_path / "concurrent.db"
    first = SqliteSessionStore(path)
    second = SqliteSessionStore(path)
    session = await first.create()
    try:
        await asyncio.gather(
            first.enqueue_inbox(session.id, InboxDelivery("one", _message("one"))),
            second.enqueue_inbox(session.id, InboxDelivery("two", _message("two"))),
        )
        rows = await first.commit_inbox(session.id, after_seq=0)
        assert {row.message.content[0].text for row in rows} == {"one", "two"}  # type: ignore[union-attr]
        assert [row.seq for row in rows] == [1, 2]
    finally:
        await first.close()
        await second.close()


async def test_sqlite_additive_migration_from_pre_inbox_database(tmp_path) -> None:
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "create table sessions (id text primary key, created_at text not null, "
        "updated_at text not null, meta text not null, invoked_skills text not null default '[]')"
    )
    conn.execute(
        "create table messages (session_id text not null, seq integer not null, "
        "appended_at text not null, message text not null, primary key (session_id, seq))"
    )
    conn.execute("insert into sessions values ('legacy', 'old', 'old', '{}', '[]')")
    conn.execute(
        "insert into messages values ('legacy', 1, 'old', ?)",
        (json.dumps({"role": "user", "content": [{"type": "text", "text": "kept"}]}),),
    )
    conn.commit()
    conn.close()

    store = SqliteSessionStore(path)
    try:
        await store.enqueue_inbox("legacy", InboxDelivery("new", _message("new")))
        rows = await store.commit_inbox("legacy", after_seq=0)
        assert [row.seq for row in rows] == [1, 2]
        assert [row.message.content[0].text for row in rows] == ["kept", "new"]  # type: ignore[union-attr]
    finally:
        await store.close()


class _AsyncContext:
    def __init__(self, value: object) -> None:
        self.value = value

    async def __aenter__(self) -> object:
        return self.value

    async def __aexit__(self, *args: object) -> None:
        return None


class _InboxPgConnection:
    def __init__(self, *, inserted_digest: str | None, existing_digest: str | None = None) -> None:
        self.inserted_digest = inserted_digest
        self.existing_digest = existing_digest
        self.queries: list[str] = []

    def transaction(self) -> _AsyncContext:
        return _AsyncContext(self)

    async def fetchval(self, query: str, *args: object) -> object:
        normalized = " ".join(query.split())
        self.queries.append(normalized)
        if "FROM sessions" in normalized:
            return args[0]
        if "INSERT INTO session_inbox" in normalized:
            return self.inserted_digest
        if "SELECT digest FROM session_inbox" in normalized:
            return self.existing_digest
        raise AssertionError(f"unexpected query: {normalized}")


class _InboxPgPool:
    def __init__(self, connection: _InboxPgConnection) -> None:
        self.connection = connection

    def acquire(self) -> _AsyncContext:
        return _AsyncContext(self.connection)


def _postgres_store(connection: _InboxPgConnection) -> PostgresSessionStore:
    store = PostgresSessionStore.__new__(PostgresSessionStore)
    store._initialized = True
    store._pool = _InboxPgPool(connection)
    return store


async def test_postgres_inbox_schema_and_enqueue_locking() -> None:
    delivery = InboxDelivery("delivery", _message("payload"))
    connection = _InboxPgConnection(inserted_digest=delivery.digest)

    await _postgres_store(connection).enqueue_inbox("session", delivery)

    assert "CREATE TABLE IF NOT EXISTS session_inbox" in _SCHEMA
    assert "FOR UPDATE" in connection.queries[0]
    assert "INSERT INTO session_inbox" in connection.queries[1]


async def test_postgres_inbox_conflict_fails_closed() -> None:
    delivery = InboxDelivery("delivery", _message("payload"))
    connection = _InboxPgConnection(inserted_digest=None, existing_digest="different")

    with pytest.raises(ConfigError, match="conflicting inbox delivery"):
        await _postgres_store(connection).enqueue_inbox("session", delivery)
