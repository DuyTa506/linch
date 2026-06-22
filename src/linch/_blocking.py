"""Bounded, daemon-thread offload for blocking work.

The core loop must never run blocking disk/DB/CPU work directly on the event
loop thread. ``asyncio.to_thread`` uses the default executor, whose non-daemon
workers can keep the interpreter alive at teardown in the managed test sandbox.

``run_blocking`` starts a daemon worker and polls its completion with a short
async sleep. That keeps teardown safe and avoids relying on cross-thread event
loop wakeups, which are not reliable in every host environment.
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
        done = threading.Event()
        result: list[T] = []
        error: list[BaseException] = []

        def _target() -> None:
            try:
                result.append(fn(*args, **kwargs))
            except BaseException as exc:  # noqa: BLE001 - propagated to awaiter
                error.append(exc)
            finally:
                done.set()

        threading.Thread(target=_target, name="linch-blocking", daemon=True).start()
        while not done.is_set():
            await asyncio.sleep(0.001)
        if error:
            raise error[0]
        return result[0]
