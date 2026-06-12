"""Bounded, daemon-thread offload for blocking work.

The core loop must never run blocking disk/DB/CPU work directly on the event
loop thread.  ``asyncio.to_thread`` dispatches onto the default executor whose
*non-daemon* worker threads can keep the interpreter (and the managed test
sandbox) alive at teardown, and an unbounded ``threading.Thread`` per call has
no backpressure.

``run_blocking`` threads the needle: each call runs on a fresh *daemon* thread
(never blocks teardown) and a per-loop semaphore caps how many run at once
(backpressure).  Wakeup is via ``loop.call_soon_threadsafe`` so the awaiting
coroutine resumes reliably.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")

# Cap on concurrently-offloaded blocking calls per event loop.  Mirrors the
# default thread-pool sizing intent without sharing a global pool across loops.
_MAX_CONCURRENCY = 32


def _loop_semaphore(loop: asyncio.AbstractEventLoop) -> asyncio.Semaphore:
    sem = getattr(loop, "_linch_blocking_sem", None)
    if sem is None:
        sem = asyncio.Semaphore(_MAX_CONCURRENCY)
        try:
            loop._linch_blocking_sem = sem  # type: ignore[attr-defined]
        except Exception:
            pass
    return sem


async def run_blocking(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run ``fn(*args, **kwargs)`` on a bounded daemon thread; return its result.

    Propagates any exception ``fn`` raises to the awaiter.  If the awaiting
    coroutine is cancelled the daemon thread still runs to completion (the same
    contract as ``asyncio.to_thread``), but it never blocks interpreter exit.
    """
    loop = asyncio.get_running_loop()
    sem = _loop_semaphore(loop)
    async with sem:
        fut: asyncio.Future[T] = loop.create_future()

        def _target() -> None:
            try:
                value = fn(*args, **kwargs)
            except BaseException as exc:  # noqa: BLE001 - propagated to awaiter
                loop.call_soon_threadsafe(_safe_set_exception, fut, exc)
                return
            loop.call_soon_threadsafe(_safe_set_result, fut, value)

        threading.Thread(target=_target, name="linch-blocking", daemon=True).start()
        return await fut


def _safe_set_result(fut: asyncio.Future[Any], value: Any) -> None:
    if not fut.done():
        fut.set_result(value)


def _safe_set_exception(fut: asyncio.Future[Any], exc: BaseException) -> None:
    if not fut.done():
        fut.set_exception(exc)
