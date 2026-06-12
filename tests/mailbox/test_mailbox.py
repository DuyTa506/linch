"""Unit tests for the peer-addressable mailbox substrate and correlation FSM."""

from __future__ import annotations

import asyncio

import pytest

from linch.mailbox import (
    Correlator,
    InMemoryMailbox,
    MailboxMessage,
)


def _msg(sender: str, recipient: str, content: str = "hi", **kw: object) -> MailboxMessage:
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
