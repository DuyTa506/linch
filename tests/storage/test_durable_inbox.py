from __future__ import annotations

import asyncio

import pytest

from linch.durability import DurabilityOptions, InboxDelivery, JsonValue, _strict_json
from linch.errors import ConfigError
from linch.sessions import InMemorySessionStore
from linch.sessions.postgres import _SCHEMA, PostgresSessionStore
from linch.types import Message, TextBlock


def _message(text: str) -> Message:
    return Message(role="user", content=[TextBlock(text=text)])


@pytest.fixture(params=["memory", "postgres"])
async def inbox_store(request, pg_dsn):
    store = InMemorySessionStore() if request.param == "memory" else PostgresSessionStore(pg_dsn)
    try:
        yield store
    finally:
        await store.close()


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


@pytest.mark.parametrize("mappings", [False, True], ids=["lists", "dicts"])
async def test_inbox_delivery_rejects_excessive_metadata_nesting(
    inbox_store, mappings: bool
) -> None:
    session = await inbox_store.create()
    value: object = "leaf"
    for _ in range(1500):
        value = {"child": value} if mappings else [value]

    with pytest.raises(ConfigError, match="maximum nesting depth"):
        InboxDelivery(
            f"deep-{session.id}",
            _message("bad"),
            metadata={"payload": value},  # type: ignore[dict-item]
        )


@pytest.mark.parametrize(("depth", "reject"), [(100, False), (101, True)])
async def test_inbox_delivery_matches_strict_json_depth_boundary(
    inbox_store, depth, reject
) -> None:
    session = await inbox_store.create()
    payload: object = "leaf"
    for _ in range(depth):
        payload = {"child": payload}
    delivery_payload: object = "leaf"
    for _ in range(depth - 1):
        delivery_payload = {"child": delivery_payload}

    if reject:
        with pytest.raises(ConfigError, match="maximum nesting depth 100"):
            _strict_json(payload, label="boundary")
        with pytest.raises(ConfigError, match="maximum nesting depth 100"):
            InboxDelivery(
                f"boundary-{depth}-{session.id}",
                _message("bad"),
                metadata=delivery_payload,  # type: ignore[arg-type]
            )
    else:
        _strict_json(payload, label="boundary")
        InboxDelivery(
            f"boundary-{depth}-{session.id}",
            _message("good"),
            metadata=delivery_payload,  # type: ignore[arg-type]
        )


async def test_store_inbox_fifo_dedupe_conflict_and_response_loss(inbox_store) -> None:
    store = inbox_store
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


async def test_commit_returns_non_inbox_messages_newer_than_cursor(inbox_store) -> None:
    store = inbox_store
    session = await store.create()
    await store.append_messages(session.id, [_message("direct")])

    rows = await store.commit_inbox(session.id, after_seq=0)

    assert [row.message.content[0].text for row in rows] == ["direct"]  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ("{", ValueError),
        ("[]", ConfigError),
        ('{"role":"user","content":[{"type":"unknown"}]}', ValueError),
    ],
)
@pytest.mark.parametrize("after_seq", [0, 100])
async def test_inbox_decode_failure_leaves_batch_pending(
    inbox_store, payload, error, after_seq
) -> None:
    store = inbox_store
    session = await store.create()
    await store.enqueue_inbox(session.id, InboxDelivery("first", _message("first")))
    await store.enqueue_inbox(session.id, InboxDelivery("second", _message("second")))
    if isinstance(store, InMemorySessionStore):
        store._inbox[session.id]["second"].message_json = payload
    else:
        pool = await store._ensure()
        await pool.execute(
            "UPDATE session_inbox SET message = $1 WHERE session_id = $2 AND delivery_id = $3",
            payload,
            session.id,
            "second",
        )

    with pytest.raises(error):
        await store.commit_inbox(session.id, after_seq=after_seq)

    if isinstance(store, InMemorySessionStore):
        message_count = len(store._messages[session.id])
        pending_count = sum(row.message_seq is None for row in store._inbox[session.id].values())
    else:
        message_count = await pool.fetchval(
            "SELECT count(*) FROM messages WHERE session_id = $1", session.id
        )
        pending_count = await pool.fetchval(
            "SELECT count(*) FROM session_inbox WHERE session_id = $1 AND message_seq IS NULL",
            session.id,
        )
    assert (message_count, pending_count) == (0, 2)


@pytest.mark.parametrize("replay", [False, True])
async def test_inbox_results_are_detached(inbox_store, replay) -> None:
    store = inbox_store
    session = await store.create()
    await store.enqueue_inbox(session.id, InboxDelivery("first", _message("before")))
    if replay:
        await store.commit_inbox(session.id, after_seq=0)
    rows = await store.commit_inbox(session.id, after_seq=0)
    rows[0].seq = 99
    rows[0].message.content[0].text = "caller mutation"
    reloaded = await store.load_messages(session.id)
    assert reloaded[0].seq == 1
    assert reloaded[0].message == _message("before")
    assert (await store.commit_inbox(session.id, after_seq=0))[0].message == _message("before")


async def test_store_inbox_unknown_session_and_cursor_validation(inbox_store) -> None:
    store = inbox_store
    with pytest.raises(KeyError):
        await store.enqueue_inbox("missing", InboxDelivery("one", _message("x")))
    with pytest.raises(KeyError):
        await store.commit_inbox("missing", after_seq=0)

    session = await store.create()
    with pytest.raises(ConfigError, match="non-negative integer"):
        await store.commit_inbox(session.id, after_seq=-1)
    with pytest.raises(ConfigError, match="non-negative integer"):
        await store.commit_inbox(session.id, after_seq=True)


async def test_postgres_receipt_prunes_payload_and_survives_reopen(
    pg_dsn,
) -> None:
    store = PostgresSessionStore(pg_dsn)
    session = await store.create()
    delivery = InboxDelivery("stable", _message("payload"), metadata={"large": "x" * 1000})
    await store.enqueue_inbox(session.id, delivery)
    await store.commit_inbox(session.id, after_seq=0)
    pool = await store._ensure()
    receipt = await pool.fetchrow(
        "SELECT digest, message, source, metadata_json, message_seq "
        "FROM session_inbox WHERE session_id = $1 AND delivery_id = $2",
        session.id,
        delivery.delivery_id,
    )
    assert tuple(receipt) == (delivery.digest, None, None, None, 1)
    await store.close()

    reopened = PostgresSessionStore(pg_dsn)
    try:
        await reopened.enqueue_inbox(session.id, delivery)
        rows = await reopened.commit_inbox(session.id, after_seq=0)
        assert len(rows) == 1
        assert rows[0].seq == 1
    finally:
        await reopened.close()


async def test_postgres_two_connections_enqueue_without_lost_fifo_rows(
    pg_dsn,
) -> None:
    first = PostgresSessionStore(pg_dsn)
    second = PostgresSessionStore(pg_dsn)
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
