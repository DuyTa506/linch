"""Host-supplied gate around every live provider call.

Linch's reactive path only reacts *after* a provider says no: ``with_retry``
reads ``retry_after_seconds`` off a ``RateLimitError``. This is the proactive
counterpart — one ceiling the host owns, held around the two places core talks
to a provider (the turn stream and compaction).

The policy lives entirely in the host. A plain concurrency cap is six lines::

    class SemaphoreLimiter:
        def __init__(self, n: int) -> None:
            self._sem = asyncio.Semaphore(n)

        async def acquire(self, *, model: str) -> None:
            await self._sem.acquire()

        def release(self, *, model: str) -> None:
            self._sem.release()

A rate limiter simply sleeps inside ``acquire``; a distributed one awaits a
lease. Cancellation needs no special handling — a task cancelled while waiting
raises out of the ``await`` on its own.
"""

from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager, nullcontext
from typing import Any, Protocol, runtime_checkable

__all__ = ["Limiter", "provider_slot"]


@runtime_checkable
class Limiter(Protocol):
    """Bounds how many live provider calls an ``Agent`` may have in flight.

    Core calls ``acquire`` before a provider call and ``release`` in a
    ``finally``, so a raising, cancelled, or abandoned call never leaks a slot.
    A retrying call releases and re-acquires, so its slot is free while it
    backs off.
    """

    async def acquire(self, *, model: str) -> None:
        """Wait until this call may proceed. *model* allows per-model budgets."""
        ...

    def release(self, *, model: str) -> None:
        """Hand the slot back. Must not raise."""
        ...


class _SemaphoreLimiter:
    """Backs ``Agent(max_provider_concurrency=N)``.

    The semaphore is built on first acquire, not in ``__init__``: an ``Agent``
    is routinely constructed outside a running loop, and on 3.10 a semaphore
    built there binds to the wrong one.
    """

    __slots__ = ("_cap", "_sem")

    def __init__(self, cap: int) -> None:
        self._cap = cap
        self._sem: asyncio.Semaphore | None = None

    async def acquire(self, *, model: str) -> None:
        if self._sem is None:
            self._sem = asyncio.Semaphore(self._cap)
        await self._sem.acquire()

    def release(self, *, model: str) -> None:
        if self._sem is not None:
            self._sem.release()


class _Slot:
    """Async context manager pairing one ``acquire`` with one ``release``."""

    __slots__ = ("_limiter", "_model")

    def __init__(self, limiter: Limiter, model: str) -> None:
        self._limiter = limiter
        self._model = model

    async def __aenter__(self) -> None:
        await self._limiter.acquire(model=self._model)

    async def __aexit__(self, *_exc: Any) -> None:
        self._limiter.release(model=self._model)


def provider_slot(agent: Any, model: str) -> AbstractAsyncContextManager[Any]:
    """Return the gate to hold for one provider call on *agent*.

    With no limiter configured this is ``nullcontext()`` — no semaphore, no
    task, no behavior change — so the feature stays free when unused.
    """
    limiter = getattr(agent, "limiter", None)
    return nullcontext() if limiter is None else _Slot(limiter, model)
