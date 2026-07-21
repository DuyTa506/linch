"""Regression test: FileProjectStore methods must not block the event loop."""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path

from linch_studio.server import FileProjectStore


async def test_create_project_does_not_block_the_event_loop(tmp_path: Path, monkeypatch) -> None:
    store = FileProjectStore(tmp_path / "workspace")
    real_atomic_write = store._atomic_write

    def slow_atomic_write(path, data):
        time.sleep(0.2)
        real_atomic_write(path, data)

    monkeypatch.setattr(store, "_atomic_write", slow_atomic_write)

    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    heartbeat_task = asyncio.create_task(heartbeat())
    await store.create_project("demo", title="Demo")
    ticks_during_call = ticks
    heartbeat_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await heartbeat_task

    assert ticks_during_call > 0
