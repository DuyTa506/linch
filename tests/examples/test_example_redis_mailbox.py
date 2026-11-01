from __future__ import annotations

from pathlib import Path

from linch.testing import assert_mailbox_contract
from tests.support.modules import load_module

_EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "integrations" / "redis_mailbox.py"


def _load():
    return load_module(_EXAMPLE, "redis_mailbox_example")


async def test_redis_mailbox_satisfies_contract() -> None:
    example = _load()
    await assert_mailbox_contract(lambda: example.RedisMailbox(example._FakeRedis()))
