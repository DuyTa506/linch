"""Phase batching retains journal order, rollback state, and tool durability."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from linch.events import PartialAssistantEvent
from linch.loop.checkpoint import RunEventBuffer
from linch.run_store import RunCheckpoint
from linch.types import Usage


class _AtomicBufferStore:
    def __init__(self, failure: type[BaseException] | None):
        self.failure = failure
        self.buffer: RunEventBuffer | None = None
        self.tail = PartialAssistantEvent(delta={"text": "tail"})
        self.saved: list[Any] = []

    async def save_checkpoint_with_events(self, run_id, checkpoint, events, *, status="running"):
        assert self.buffer is not None
        await self.buffer.append(self.tail)
        if self.failure is not None:
            raise self.failure()
        self.saved.extend(events)
        return [8, 9]

    async def mark_failed(self, run_id, checkpoint=None, error=None, *, events=()):
        return await self.save_checkpoint_with_events(run_id, checkpoint, events, status="failed")


@pytest.mark.parametrize("status", ["running", "failed"])
@pytest.mark.parametrize("failure", [OSError, asyncio.CancelledError])
async def test_atomic_buffer_failure_retains_pending_prefix_and_concurrent_tail(
    failure, status
) -> None:
    store = _AtomicBufferStore(failure)
    buffer = RunEventBuffer(store, "run", initial_seq=4)
    store.buffer = buffer
    events = [PartialAssistantEvent(delta={"text": text}) for text in ("a", "b")]
    for event in events:
        await buffer.append(event)
    with pytest.raises(failure):
        await buffer.flush_with_checkpoint(
            RunCheckpoint("turn_complete", "hi", 0, Usage()), status=status
        )
    assert buffer.last_seq == 4
    assert buffer._pending == [*events, store.tail]
    assert store.saved == []


async def test_atomic_buffer_commits_actual_seq_and_preserves_concurrent_tail() -> None:
    store = _AtomicBufferStore(None)
    buffer = RunEventBuffer(store, "run", initial_seq=4)
    store.buffer = buffer
    events = [PartialAssistantEvent(delta={"text": text}) for text in ("a", "b")]
    for event in events:
        await buffer.append(event)
    await buffer.flush_with_checkpoint(RunCheckpoint("turn_complete", "hi", 0, Usage()))
    assert buffer.last_seq == 9
    assert buffer._pending == [store.tail]
    assert store.saved == events


class _RecordingStore:
    def __init__(self, inner):
        self.inner = inner
        self.writes = []
        self.atomic_phases = []
        self.checkpoint_phases = []
        self.event_batches = []

    def __getattr__(self, name):
        return getattr(self.inner, name)

    async def append_events(self, run_id, events):
        self.writes.append("events")
        self.event_batches.append([event.type for event in events])
        return await self.inner.append_events(run_id, events)

    async def save_checkpoint(self, run_id, checkpoint, **kwargs):
        self.writes.append("checkpoint")
        self.checkpoint_phases.append(checkpoint.phase)
        return await self.inner.save_checkpoint(run_id, checkpoint, **kwargs)

    async def mark_completed(self, run_id, checkpoint):
        self.writes.append("completed")
        return await self.inner.mark_completed(run_id, checkpoint)

    async def save_checkpoint_with_events(self, run_id, checkpoint, events, *, status="running"):
        self.writes.append("atomic")
        self.atomic_phases.append(checkpoint.phase)
        return await self.inner.save_checkpoint_with_events(
            run_id, checkpoint, events, status=status
        )


async def test_phase_atomic_run_reduces_writes_and_keeps_tool_guard_durable(pg_dsn):
    from test_run_resume import ScriptProvider, _agent, _memory_session_store

    from linch.run_stores.postgres import PostgresRunStore
    from tests.support.tools import StubTool, tool_registry

    store = _RecordingStore(PostgresRunStore(pg_dsn))
    checked = []

    class DurableTool(StubTool):
        async def execute(self, input, ctx, _store=store, _checked=checked):
            record = await _store.load_run(ctx.run_id)
            assert record.checkpoint.phase == "tool_executing"
            _checked.append(True)
            return await super().execute(input, ctx)

    tools = tool_registry(DurableTool("A"))
    agent = _agent(
        model="gpt-5",
        provider=ScriptProvider(tool_names=["A"]),
        session_store=_memory_session_store(),
        run_store=store,
        tools=tools,
        cwd=".",
        permissions={"mode": "skip-dangerous"},
    )
    try:
        session = await agent.session()
        events = []
        run_id = ""
        async for event in session.run("hello"):
            events.append(event)
            if event.type == "system":
                run_id = event.run_id
            if event.type in {"tool_call_start", "tool_call_end"}:
                assert (await store.load_events(run_id))[-1].event.type == event.type
        assert checked == [True]
        assert events[-1].subtype == "success"
        assert (await store.load_run(run_id)).status == "completed"

        assert "turn_complete" in store.atomic_phases
        assert "completed" in store.atomic_phases
        assert "provider_pending" in store.checkpoint_phases
        assert {
            "user_appended",
            "tool_batch_pending",
        }.issubset(store.atomic_phases)
    finally:
        await agent.close()


async def test_required_atomic_method_is_called_directly() -> None:
    inner = _AtomicBufferStore(None)
    buffer = RunEventBuffer(inner, "run", initial_seq=4)
    inner.buffer = buffer
    events = [PartialAssistantEvent(delta={"text": text}) for text in ("a", "b")]
    for event in events:
        await buffer.append(event)
    await buffer.flush_with_checkpoint(RunCheckpoint("turn_complete", "hi", 0, Usage()))
    assert buffer.last_seq == 9
    assert inner.saved == events


async def test_failed_atomic_write_cannot_be_split_by_finally_flush() -> None:
    class Store(_AtomicBufferStore):
        async def append_events(self, run_id, events):
            self.saved.extend(events)
            return [1]

    store = Store(asyncio.CancelledError)
    buffer = RunEventBuffer(store, "run")
    store.buffer = buffer
    await buffer.append(PartialAssistantEvent(delta={"text": "pending"}))
    with pytest.raises(asyncio.CancelledError):
        await buffer.flush_with_checkpoint(RunCheckpoint("turn_complete", "hi", 0, Usage()))
    await buffer.flush()
    assert store.saved == []
    assert len(buffer._pending) == 2
