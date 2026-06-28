"""Bounded, daemon-thread offload for blocking work.

The core loop must never run blocking disk/DB/CPU work directly on the event
loop thread. ``asyncio.to_thread`` uses the default executor, whose non-daemon
workers can keep the interpreter alive at teardown in the managed test sandbox.

``run_blocking`` starts a daemon worker and resumes the awaiter via a future
woken with ``loop.call_soon_threadsafe``.  A slow fallback wake loop guards
against runtimes where a thread-safe wakeup is lost after SQLite work in the
daemon thread; the fast path still returns as soon as the callback is delivered.
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
    """Run ``fn(*args, **kwargs)`` on a bounded daemon thread.

    Propagates any exception ``fn`` raises to the awaiter.  If the awaiting
    coroutine is cancelled, the daemon thread still runs to completion.
    """
    loop = asyncio.get_running_loop()
    sem = _loop_semaphore(loop)
    async with sem:
        fut: asyncio.Future[T] = loop.create_future()

        def _set_result(value: T) -> None:
            if not fut.done():
                fut.set_result(value)

        def _set_exception(exc: BaseException) -> None:
            if not fut.done():
                fut.set_exception(exc)

        def _target() -> None:
            try:
                value = fn(*args, **kwargs)
            except BaseException as exc:  # noqa: BLE001 - propagated to awaiter
                loop.call_soon_threadsafe(_set_exception, exc)
            else:
                loop.call_soon_threadsafe(_set_result, value)

        threading.Thread(target=_target, name="linch-blocking", daemon=True).start()
        # If the awaiter is cancelled, the daemon thread still runs to completion;
        # its later call_soon_threadsafe is a no-op on the already-cancelled future.
        # Some managed runtimes have been observed to lose the selector wakeup
        # after SQLite work in the daemon thread.  The timeout keeps those calls
        # from hanging forever while remaining dormant on the normal fast path.
        while True:
            done, _ = await asyncio.wait({fut}, timeout=0.1)
            if done:
                return fut.result()
