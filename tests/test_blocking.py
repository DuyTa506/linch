from __future__ import annotations

import asyncio
import threading

import pytest


async def test_run_blocking_returns_and_forwards_arguments() -> None:
    from linch._blocking import run_blocking

    worker_thread: threading.Thread | None = None

    def work(left: int, *, right: int) -> int:
        nonlocal worker_thread
        worker_thread = threading.current_thread()
        return left + right

    assert await run_blocking(work, 2, right=3) == 5
    assert worker_thread is not None
    assert worker_thread is not threading.current_thread()
    assert worker_thread.daemon is True


async def test_run_blocking_propagates_worker_exception() -> None:
    from linch._blocking import run_blocking

    def fail() -> None:
        raise ValueError("worker failed")

    with pytest.raises(ValueError, match="worker failed"):
        await run_blocking(fail)


async def test_run_blocking_cancellation_does_not_set_cancelled_future() -> None:
    from linch._blocking import run_blocking

    started = threading.Event()
    release = threading.Event()

    def work() -> str:
        started.set()
        release.wait(timeout=1.0)
        return "late result"

    task = asyncio.create_task(run_blocking(work))
    assert await asyncio.to_thread(started.wait, 1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    release.set()
    await asyncio.sleep(0.01)


async def test_run_blocking_keeps_concurrency_bounded_after_cancellation() -> None:
    from linch._blocking import run_blocking

    release = threading.Event()
    saturated = threading.Event()
    extra_started = threading.Event()
    guard = threading.Lock()
    active = 0
    max_active = 0

    def blocked(index: int) -> int:
        nonlocal active, max_active
        with guard:
            active += 1
            max_active = max(max_active, active)
            if active == 32:
                saturated.set()
        release.wait(timeout=2.0)
        with guard:
            active -= 1
        return index

    tasks = [asyncio.create_task(run_blocking(blocked, index)) for index in range(32)]
    assert await asyncio.to_thread(saturated.wait, 1.0)

    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    def extra() -> str:
        extra_started.set()
        return "extra"

    extra_task = asyncio.create_task(run_blocking(extra))
    await asyncio.sleep(0.02)
    assert not extra_started.is_set(), "cancelled workers released permits before finishing"
    assert max_active == 32

    release.set()
    assert await asyncio.wait_for(extra_task, timeout=1.0) == "extra"


async def test_run_blocking_cancelled_late_exception_is_consumed() -> None:
    from linch._blocking import run_blocking

    loop = asyncio.get_running_loop()
    contexts: list[dict[str, object]] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: contexts.append(context))
    started = threading.Event()
    release = threading.Event()

    def fail_late() -> None:
        started.set()
        release.wait(timeout=1.0)
        raise RuntimeError("late failure")

    try:
        task = asyncio.create_task(run_blocking(fail_late))
        assert await asyncio.to_thread(started.wait, 1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        await asyncio.sleep(0.02)
        assert contexts == []
    finally:
        loop.set_exception_handler(previous_handler)


async def test_run_blocking_fallback_timer_handles_missing_selector_wake(monkeypatch) -> None:
    """A queued callback still completes when the thread-safe wake byte is lost."""
    from linch._blocking import run_blocking

    loop = asyncio.get_running_loop()

    # BaseEventLoop.call_soon appends to the ready queue but, unlike
    # call_soon_threadsafe, does not write the selector wake byte. Calling it from
    # the worker models the observed managed-runtime failure; run_blocking's
    # already-registered timer must wake the loop and let the callback run.
    monkeypatch.setattr(loop, "call_soon_threadsafe", loop.call_soon)

    result = await asyncio.wait_for(run_blocking(lambda: 42), timeout=0.2)
    assert result == 42
