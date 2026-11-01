from __future__ import annotations

from pathlib import Path

from linch import Correlator, MailboxMessage
from linch.testing import assert_mailbox_contract
from tests.support.modules import load_module

_EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "integrations" / "redis_mailbox.py"


def _load():
    return load_module(_EXAMPLE, "redis_mailbox_example")


async def test_redis_mailbox_satisfies_contract() -> None:
    example = _load()
    await assert_mailbox_contract(lambda: example.RedisMailbox(example._FakeRedis()))


async def test_redis_mailbox_round_trips_correlation_fields() -> None:
    example = _load()
    mailbox = example.RedisMailbox(example._FakeRedis())
    correlator = Correlator()
    correlator.open("req-1")

    message = MailboxMessage(
        sender="lead",
        recipient="alice",
        content="Approved",
        type="plan_approval_response",
        request_id="req-0",
        in_reply_to="req-1",
    )
    await mailbox.send(message)
    [delivered] = await mailbox.drain("alice")

    assert delivered == message
    assert correlator.resolve(delivered)
