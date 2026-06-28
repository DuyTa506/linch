"""Smoke test for the Redis mailbox adapter example (examples/integrations/redis_mailbox.py).

Proves an external-service-shaped adapter built outside core satisfies the
`Mailbox` contract via the reusable `assert_mailbox_contract` helper — exactly
how a third-party adapter package would validate itself in CI.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from linch import MailboxMessage
from linch.testing import assert_mailbox_contract

_EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "integrations" / "redis_mailbox.py"


def _load():
    spec = importlib.util.spec_from_file_location("redis_mailbox_example", _EXAMPLE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_redis_mailbox_satisfies_contract() -> None:
    example = _load()
    await assert_mailbox_contract(lambda: example.RedisMailbox(example._FakeRedis()))


async def test_redis_mailbox_round_trips_message_fields() -> None:
    example = _load()
    mailbox = example.RedisMailbox(example._FakeRedis())

    sent = MailboxMessage(sender="a", recipient="b", content="hi", request_id="r1")
    await mailbox.send(sent)
    [got] = await mailbox.drain("b")

    assert got.id == sent.id
    assert (got.sender, got.recipient, got.content, got.request_id) == ("a", "b", "hi", "r1")
