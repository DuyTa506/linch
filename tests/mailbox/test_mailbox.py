"""Unit tests for the peer-addressable mailbox substrate and correlation FSM."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from linch.coordination.mailbox import (
    ClaimableMailbox,
    Correlator,
    InMemoryMailbox,
    MailboxMessage,
)


def _msg(sender: str, recipient: str, content: str = "hi", **kw: Any) -> MailboxMessage:
    return MailboxMessage(sender=sender, recipient=recipient, content=content, **kw)


def test_message_id_is_generated_unless_explicit() -> None:
    m1 = MailboxMessage(sender="a", recipient="b", content="x")
    m2 = MailboxMessage(sender="a", recipient="b", content="x")
    assert m1.id and m2.id
    assert m1.id != m2.id
    m = MailboxMessage(sender="a", recipient="b", content="x", id="fixed")
    assert m.id == "fixed"


# --- Correlation FSM ---------------------------------------------------------


def test_resolve_matches_by_request_id_and_first_wins() -> None:
    c = Correlator()
    c.open("req-1")
    assert not c.is_resolved("req-1")
    first = MailboxMessage(sender="b", recipient="a", content="one", in_reply_to="req-1")
    second = MailboxMessage(sender="b", recipient="a", content="two", in_reply_to="req-1")

    assert c.resolve(first) is True
    assert c.is_resolved("req-1")
    assert c.resolve(second) is False
    assert c.response("req-1") is first


def test_unmatched_responses_resolve_nothing() -> None:
    c = Correlator()
    ghost = MailboxMessage(sender="b", recipient="a", content="pong", in_reply_to="ghost")
    assert c.resolve(ghost) is False
    assert c.response("ghost") is None

    c.open("req-1")
    bare = MailboxMessage(sender="b", recipient="a", content="pong")
    assert c.resolve(bare) is False
    assert not c.is_resolved("req-1")


def test_pending_lists_unresolved_requests() -> None:
    c = Correlator()
    c.open("a")
    c.open("b")
    c.resolve(MailboxMessage(sender="x", recipient="y", content="", in_reply_to="a"))
    assert c.pending() == ["b"]


# --- Concurrent delivery and leases ------------------------------------------


@pytest.mark.asyncio
async def test_memory_mailbox_drain_is_fifo_per_recipient_and_destructive() -> None:
    box = InMemoryMailbox()
    for i in range(5):
        await box.send(_msg("a", "b", str(i)))

    assert await box.drain("c") == []
    assert await box.drain("nobody") == []
    first = await box.drain("b")
    second = await box.drain("b")

    assert [m.content for m in first] == ["0", "1", "2", "3", "4"]
    assert second == []


@pytest.mark.asyncio
async def test_memory_mailbox_concurrent_sends_dont_drop_messages(tmp_path) -> None:
    box = InMemoryMailbox()
    n = 200

    await asyncio.gather(*(box.send(_msg("a", "b", str(i))) for i in range(n)))

    drained = await box.drain("b")

    assert sorted(int(m.content) for m in drained) == list(range(n))


@pytest.mark.parametrize("backend", ["memory"])
async def test_claim_is_fifo_and_valid_oldest_lease_blocks_newer(
    backend: str, tmp_path: Any
) -> None:
    box: ClaimableMailbox
    box = InMemoryMailbox()
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


@pytest.mark.parametrize("backend", ["memory"])
async def test_claim_release_expiry_and_stale_ack_are_token_fenced(
    backend: str, tmp_path: Any
) -> None:
    box: ClaimableMailbox
    box = InMemoryMailbox()
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
