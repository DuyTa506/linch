"""Redis-backed Mailbox adapter (ROADMAP P8).

External queue/store backends (Redis, SQS, Postgres, ...) are deployment choices,
so they live in examples and optional packages — *not* in core. Core ships only
the `Mailbox` protocol; this is one implementation of it, kept honest by running
the reusable `assert_mailbox_contract` compliance check from `linch.testing`.

One Redis list per recipient (`<prefix><recipient>`):
  - ``send``  → ``RPUSH`` (append; FIFO).
  - ``drain`` → an *atomic* ``LRANGE 0 -1`` + ``DEL`` so two concurrent drains
    never deliver the same message twice. In production this is one Lua script
    (or a ``MULTI``/``EXEC`` pipeline); real Redis runs it atomically because it
    is single-threaded.

A tiny in-memory fake stands in for ``redis.asyncio.Redis`` so the example and its
test run offline. For real use, drop in::

    import redis.asyncio as redis
    mailbox = RedisMailbox(redis.from_url("redis://localhost:6379"))

Run:
    python examples/integrations/redis_mailbox.py
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from linch import MailboxMessage
from linch.testing import assert_mailbox_contract


def _encode(message: MailboxMessage) -> str:
    return json.dumps(
        {
            "id": message.id,
            "sender": message.sender,
            "recipient": message.recipient,
            "content": message.content,
            "request_id": message.request_id,
        }
    )


def _decode(raw: str) -> MailboxMessage:
    data = json.loads(raw)
    return MailboxMessage(
        sender=data["sender"],
        recipient=data["recipient"],
        content=data["content"],
        request_id=data.get("request_id"),
        id=data["id"],
    )


class _FakeRedis:
    """Minimal in-memory stand-in for ``redis.asyncio.Redis`` (list ops only).

    Real Redis executes each command atomically on a single thread; we emulate
    that with one lock so the atomic-drain pattern behaves like production.
    """

    def __init__(self) -> None:
        self._data: dict[str, list[str]] = {}
        self._lock = asyncio.Lock()

    async def rpush(self, key: str, value: str) -> int:
        async with self._lock:
            self._data.setdefault(key, []).append(value)
            return len(self._data[key])

    async def drain_atomic(self, key: str) -> list[str]:
        # Stands in for: EVAL "local v=redis.call('LRANGE',KEYS[1],0,-1);
        #                       redis.call('DEL',KEYS[1]); return v" 1 <key>
        async with self._lock:
            return self._data.pop(key, [])


class RedisMailbox:
    """A :class:`linch.MailboxMessage` mailbox backed by Redis lists."""

    name = "redis"

    def __init__(self, client: Any, *, prefix: str = "linch:mailbox:") -> None:
        self._client = client
        self._prefix = prefix

    def _key(self, recipient: str) -> str:
        return f"{self._prefix}{recipient}"

    async def send(self, message: MailboxMessage) -> None:
        await self._client.rpush(self._key(message.recipient), _encode(message))

    async def drain(self, recipient: str) -> list[MailboxMessage]:
        raw = await self._client.drain_atomic(self._key(recipient))
        return [_decode(item) for item in raw]


async def main() -> None:
    mailbox = RedisMailbox(_FakeRedis())

    await mailbox.send(MailboxMessage(sender="planner", recipient="worker", content="task A"))
    await mailbox.send(MailboxMessage(sender="planner", recipient="worker", content="task B"))
    delivered = await mailbox.drain("worker")
    print("drained:", [m.content for m in delivered])
    print("drain again (destructive):", await mailbox.drain("worker"))

    # Prove the adapter satisfies the Mailbox contract Linch's coordination
    # layer depends on — the same check a third-party package would run in CI.
    await assert_mailbox_contract(lambda: RedisMailbox(_FakeRedis()))
    print("assert_mailbox_contract: PASSED")


if __name__ == "__main__":
    asyncio.run(main())
