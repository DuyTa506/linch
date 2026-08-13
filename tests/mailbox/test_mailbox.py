"""Unit tests for the peer-addressable mailbox substrate and correlation FSM."""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any

import pytest

from linch.coordination.mailbox import (
    ClaimableMailbox,
    Correlator,
    InMemoryMailbox,
    MailboxMessage,
    SqliteMailbox,
)


def _msg(sender: str, recipient: str, content: str = "hi", **kw: Any) -> MailboxMessage:
    return MailboxMessage(sender=sender, recipient=recipient, content=content, **kw)


@pytest.mark.asyncio
async def test_send_then_drain_delivers_message() -> None:
    box = InMemoryMailbox()
    await box.send(_msg("a", "b", "ping"))

    drained = await box.drain("b")

    assert [m.content for m in drained] == ["ping"]
    # Draining again yields nothing — drain is destructive/atomic.
    assert await box.drain("b") == []


@pytest.mark.asyncio
async def test_drain_is_isolated_per_recipient() -> None:
    box = InMemoryMailbox()
    await box.send(_msg("a", "b", "for-b"))

    assert await box.drain("c") == []
    assert [m.content for m in await box.drain("b")] == ["for-b"]


@pytest.mark.asyncio
async def test_drain_unknown_recipient_returns_empty() -> None:
    box = InMemoryMailbox()
    assert await box.drain("nobody") == []


@pytest.mark.asyncio
async def test_concurrent_sends_dont_drop_messages() -> None:
    box = InMemoryMailbox()
    n = 200
    await asyncio.gather(*(box.send(_msg("a", "b", str(i))) for i in range(n)))

    drained = await box.drain("b")

    assert sorted(int(m.content) for m in drained) == list(range(n))


@pytest.mark.asyncio
async def test_send_preserves_fifo_order() -> None:
    box = InMemoryMailbox()
    for i in range(5):
        await box.send(_msg("a", "b", str(i)))

    drained = await box.drain("b")

    assert [m.content for m in drained] == ["0", "1", "2", "3", "4"]


def test_message_assigns_id_when_absent() -> None:
    m1 = MailboxMessage(sender="a", recipient="b", content="x")
    m2 = MailboxMessage(sender="a", recipient="b", content="x")
    assert m1.id and m2.id
    assert m1.id != m2.id


def test_message_keeps_explicit_id() -> None:
    m = MailboxMessage(sender="a", recipient="b", content="x", id="fixed")
    assert m.id == "fixed"


# --- Correlation FSM ---------------------------------------------------------


def test_open_then_resolve_matches_by_request_id() -> None:
    c = Correlator()
    c.open("req-1")
    assert not c.is_resolved("req-1")

    response = MailboxMessage(sender="b", recipient="a", content="pong", in_reply_to="req-1")
    matched = c.resolve(response)

    assert matched is True
    assert c.is_resolved("req-1")
    assert c.response("req-1") is response


def test_resolve_without_open_returns_false() -> None:
    c = Correlator()
    response = MailboxMessage(sender="b", recipient="a", content="pong", in_reply_to="ghost")
    assert c.resolve(response) is False


def test_resolve_response_without_in_reply_to_returns_false() -> None:
    c = Correlator()
    c.open("req-1")
    bare = MailboxMessage(sender="b", recipient="a", content="pong")
    assert c.resolve(bare) is False
    assert not c.is_resolved("req-1")


def test_double_resolve_is_idempotent_first_wins() -> None:
    c = Correlator()
    c.open("req-1")
    first = MailboxMessage(sender="b", recipient="a", content="one", in_reply_to="req-1")
    second = MailboxMessage(sender="b", recipient="a", content="two", in_reply_to="req-1")

    assert c.resolve(first) is True
    assert c.resolve(second) is False
    assert c.response("req-1") is first


def test_pending_lists_unresolved_requests() -> None:
    c = Correlator()
    c.open("a")
    c.open("b")
    c.resolve(MailboxMessage(sender="x", recipient="y", content="", in_reply_to="a"))
    assert c.pending() == ["b"]


def test_response_unknown_request_is_none() -> None:
    c = Correlator()
    assert c.response("nope") is None


# --- Durable SQLite mailbox --------------------------------------------------


@pytest.mark.asyncio
async def test_sqlite_mailbox_persists_across_reopen(tmp_path) -> None:
    path = tmp_path / "mailbox.db"
    first = SqliteMailbox(path)
    await first.send(
        MailboxMessage(
            sender="alice",
            recipient="bob",
            content="persisted",
            type="question",
            request_id="req-1",
        )
    )
    await first.aclose()

    second = SqliteMailbox(path)
    drained = await second.drain("bob")
    await second.aclose()

    assert len(drained) == 1
    assert drained[0].sender == "alice"
    assert drained[0].content == "persisted"
    assert drained[0].type == "question"
    assert drained[0].request_id == "req-1"


@pytest.mark.asyncio
async def test_sqlite_mailbox_drain_is_destructive(tmp_path) -> None:
    box = SqliteMailbox(tmp_path / "mailbox.db")
    await box.send(_msg("a", "b", "one"))

    first = await box.drain("b")
    second = await box.drain("b")
    await box.aclose()

    assert [m.content for m in first] == ["one"]
    assert second == []


@pytest.mark.asyncio
async def test_sqlite_mailbox_preserves_fifo_order(tmp_path) -> None:
    box = SqliteMailbox(tmp_path / "mailbox.db")
    for i in range(5):
        await box.send(_msg("a", "b", str(i)))

    drained = await box.drain("b")
    await box.aclose()

    assert [m.content for m in drained] == ["0", "1", "2", "3", "4"]


@pytest.mark.asyncio
async def test_sqlite_mailbox_concurrent_sends_dont_drop_messages(tmp_path) -> None:
    box = SqliteMailbox(tmp_path / "mailbox.db")
    n = 100

    await asyncio.gather(*(box.send(_msg("a", "b", str(i))) for i in range(n)))

    drained = await box.drain("b")
    await box.aclose()

    assert sorted(int(m.content) for m in drained) == list(range(n))


@pytest.mark.asyncio
async def test_sqlite_mailbox_concurrent_drains_deliver_once(tmp_path) -> None:
    path = tmp_path / "mailbox.db"
    writer = SqliteMailbox(path)
    for i in range(20):
        await writer.send(_msg("a", "b", str(i)))
    await writer.aclose()

    first = SqliteMailbox(path)
    second = SqliteMailbox(path)
    drained_a, drained_b = await asyncio.gather(first.drain("b"), second.drain("b"))
    await first.aclose()
    await second.aclose()

    contents = [m.content for m in drained_a + drained_b]
    assert sorted(int(content) for content in contents) == list(range(20))
    assert len(drained_a) in {0, 20}
    assert len(drained_b) in {0, 20}


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_claim_is_fifo_and_valid_oldest_lease_blocks_newer(
    backend: str, tmp_path: Any
) -> None:
    box: ClaimableMailbox
    if backend == "memory":
        box = InMemoryMailbox()
    else:
        box = SqliteMailbox(tmp_path / "leased-mailbox.db")
    try:
        for index in range(3):
            await box.send(_msg("a", "b", str(index), id=f"m-{index}"))

        first = await box.claim("b", owner="one", lease_s=30, now=100, limit=1)
        blocked = await box.claim("b", owner="two", lease_s=30, now=101)
        assert [claim.message.content for claim in first] == ["0"]
        assert blocked == []

        await box.ack(first)
        remaining = await box.claim("b", owner="two", lease_s=30, now=101)
        assert [claim.message.content for claim in remaining] == ["1", "2"]
    finally:
        close = getattr(box, "aclose", None)
        if close is not None:
            await close()


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_claim_release_expiry_and_stale_ack_are_token_fenced(
    backend: str, tmp_path: Any
) -> None:
    box: ClaimableMailbox
    if backend == "memory":
        box = InMemoryMailbox()
    else:
        box = SqliteMailbox(tmp_path / "leased-mailbox.db")
    try:
        await box.send(_msg("a", "b", "payload", id="stable-message"))
        original = await box.claim("b", owner="one", lease_s=10, now=100)
        assert await box.claim("b", owner="two", lease_s=10, now=109) == []

        reclaimed = await box.claim("b", owner="two", lease_s=10, now=110)
        assert reclaimed[0].message.id == original[0].message.id
        assert reclaimed[0].token != original[0].token

        await box.ack(original)
        assert await box.claim("b", owner="three", lease_s=10, now=111) == []
        await box.release(reclaimed)
        released = await box.claim("b", owner="three", lease_s=10, now=111)
        assert [claim.message.id for claim in released] == ["stable-message"]
        await box.ack(released)
        assert await box.drain("b") == []
    finally:
        close = getattr(box, "aclose", None)
        if close is not None:
            await close()


async def test_sqlite_claim_survives_reopen_and_is_reclaimed_after_expiry(tmp_path: Any) -> None:
    path = tmp_path / "leased-mailbox.db"
    first = SqliteMailbox(path)
    await first.send(_msg("a", "b", "persisted", id="persisted-id"))
    claimed = await first.claim("b", owner="one", lease_s=10, now=100)
    await first.aclose()

    second = SqliteMailbox(path)
    try:
        assert await second.claim("b", owner="two", lease_s=10, now=109) == []
        reclaimed = await second.claim("b", owner="two", lease_s=10, now=110)
        assert reclaimed[0].message.id == claimed[0].message.id
        assert reclaimed[0].token != claimed[0].token
    finally:
        await second.aclose()


async def test_sqlite_mailbox_adds_lease_schema_to_legacy_database(tmp_path: Any) -> None:
    path = tmp_path / "legacy-mailbox.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE mailbox_messages (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            sender TEXT NOT NULL,
            recipient TEXT NOT NULL,
            content TEXT NOT NULL,
            type TEXT NOT NULL,
            request_id TEXT,
            in_reply_to TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO mailbox_messages(
            id, sender, recipient, content, type, request_id, in_reply_to
        ) VALUES ('legacy', 'a', 'b', 'old', 'message', NULL, NULL)
        """
    )
    conn.commit()
    conn.close()

    box = SqliteMailbox(path)
    try:
        claims = await box.claim("b", owner="migration", lease_s=30, now=100)
        assert [claim.message.id for claim in claims] == ["legacy"]
    finally:
        await box.aclose()
