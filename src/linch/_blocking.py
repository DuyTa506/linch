"""Bounded, daemon-thread offload for blocking work.

The core loop must never run blocking disk/DB/CPU work directly on the event
loop thread. ``asyncio.to_thread`` uses the default executor, whose non-daemon
workers can keep the interpreter alive at teardown in the managed test sandbox.

``run_blocking`` starts a daemon worker and resumes the awaiter via a future
woken with ``loop.call_soon_threadsafe``.  A dormant event-loop timer guards
against runtimes where the selector wakeup is lost after SQLite work in the
daemon thread; the normal path directly awaits the future and cancels the timer
before it fires.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import Any, TypeVar, cast

T = TypeVar("T")

# Cap on concurrently-offloaded blocking calls per event loop.  Mirrors the
# default thread-pool sizing intent without sharing a global pool across loops.
_MAX_CONCURRENCY = 32
_FALLBACK_WAKE_INITIAL_SECONDS = 0.001
_FALLBACK_WAKE_MAX_SECONDS = 0.010


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
    coroutine is cancelled, the daemon thread still runs to completion and
    retains its concurrency permit until it finishes.
    """
    loop = asyncio.get_running_loop()
    sem = _loop_semaphore(loop)
    await sem.acquire()
    permit_owned = True
    try:
        fut: asyncio.Future[T] = loop.create_future()
        worker_finished = False
        wake_handle: asyncio.TimerHandle | None = None

        def _complete(value: T | None, exc: BaseException | None) -> None:
            nonlocal worker_finished
            worker_finished = True
            sem.release()
            if wake_handle is not None:
                wake_handle.cancel()
            if not fut.done():
                if exc is not None:
                    fut.set_exception(exc)
                else:
                    fut.set_result(cast(T, value))

        def _target() -> None:
            try:
                value = fn(*args, **kwargs)
            except BaseException as exc:  # noqa: BLE001 - propagated to awaiter
                try:
                    loop.call_soon_threadsafe(_complete, None, exc)
                except RuntimeError:
                    # The awaiter can disappear because its event loop was
                    # closed while this daemon thread was still finishing.
                    if not loop.is_closed():
                        raise
            else:
                try:
                    loop.call_soon_threadsafe(_complete, value, None)
                except RuntimeError:
                    if not loop.is_closed():
                        raise

        worker = threading.Thread(target=_target, name="linch-blocking", daemon=True)
        worker.start()
        permit_owned = False

        # If the awaiter is cancelled, the daemon thread still runs to completion;
        # its later call_soon_threadsafe is a no-op on the cancelled future. Some
        # managed runtimes have lost the selector wake byte after SQLite work.
        # Keeping a short timer registered gives the selector a bounded fallback
        # deadline without wrapping every result in asyncio.wait(timeout=...),
        # which imposed the full polling interval on otherwise completed calls.
        wake_interval = _FALLBACK_WAKE_INITIAL_SECONDS

        def _fallback_wake() -> None:
            nonlocal wake_handle, wake_interval
            if not worker_finished:
                wake_interval = min(wake_interval * 2, _FALLBACK_WAKE_MAX_SECONDS)
                wake_handle = loop.call_later(
                    wake_interval,
                    _fallback_wake,
                )

        wake_handle = loop.call_later(
            wake_interval,
            _fallback_wake,
        )
        try:
            return await fut
        finally:
            # A cancelled awaiter still leaves its daemon worker running and
            # holding a semaphore permit. Keep the guard alive until `_complete`
            # releases that permit, otherwise a lost selector wake can strand it.
            if worker_finished and wake_handle is not None:
                wake_handle.cancel()
    except BaseException:
        # Before the worker starts, this function owns the permit. Afterwards,
        # `_complete` owns it and cancellation must not release it early.
        if permit_owned:
            sem.release()
        raise
