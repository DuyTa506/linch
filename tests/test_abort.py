from __future__ import annotations

import asyncio

import pytest

from linch.abort import AbortContext, any_signal


async def _wait_for(predicate, timeout: float = 1.0) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


@pytest.mark.asyncio
async def test_merged_aborts_when_input_aborts_and_watcher_terminates() -> None:
    a = AbortContext()
    b = AbortContext()
    merged = any_signal(a, b)

    assert not merged.aborted

    a.abort()

    # merged should become aborted shortly after an input aborts
    assert await _wait_for(lambda: merged.aborted)

    # the internal watcher must terminate (not park forever / leak)
    assert merged._watch_task is not None
    assert await _wait_for(lambda: merged._watch_task.done())


@pytest.mark.asyncio
async def test_close_cancels_watcher_when_no_abort_fires() -> None:
    a = AbortContext()
    b = AbortContext()
    merged = any_signal(a, b)

    task = merged._watch_task
    assert task is not None

    # let the watcher start parking on the input events
    await asyncio.sleep(0)
    assert not task.done()

    # cleanup path must release the watcher
    merged.close()

    assert await _wait_for(lambda: task.done())

    # no pending Event.wait futures should dangle: the input events have no
    # remaining waiters once the watcher is cleaned up.
    assert not a._event._waiters
    assert not b._event._waiters
