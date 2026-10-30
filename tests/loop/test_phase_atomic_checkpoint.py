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

    async def save_checkpoint_with_events(self, run_id, checkpoint, events, **kwargs):
        assert self.buffer is not None
        await self.buffer.append(self.tail)
        if self.failure is not None:
            raise self.failure()
        prepare = kwargs.get("prepare_checkpoint")
        if prepare is not None:
            prepare(9)
        self.saved.extend(events)
        return None, [8, 9]


@pytest.mark.parametrize("failure", [OSError, asyncio.CancelledError])
async def test_atomic_buffer_failure_retains_pending_prefix_and_concurrent_tail(failure) -> None:
    store = _AtomicBufferStore(failure)
    buffer = RunEventBuffer(store, "run", initial_seq=4)
    store.buffer = buffer
    events = [PartialAssistantEvent(delta={"text": text}) for text in ("a", "b")]
    for event in events:
        await buffer.append(event)
    capability = getattr(buffer, "flush_with_checkpoint", None)
    assert callable(capability), "event buffer must support optional phase-atomic flush"
    with pytest.raises(failure):
        await capability(RunCheckpoint("turn_complete", "hi", 0, Usage()))
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
    bound = []
    capability = getattr(buffer, "flush_with_checkpoint", None)
    assert callable(capability), "event buffer must support optional phase-atomic flush"
    assert await capability(
        RunCheckpoint("turn_complete", "hi", 0, Usage()), prepare_checkpoint=bound.append
    )
    assert bound == [9]
    assert buffer.last_seq == 9
    assert buffer._pending == [store.tail]
    assert store.saved == events


class _RecordingStore:
    def __init__(self, inner, *, atomic=True, fail_after_prepare=False):
        self.inner, self.atomic, self.fail_after_prepare = inner, atomic, fail_after_prepare
        self.writes = []
        self.atomic_phases = []
        self.event_batches = []

    def __getattr__(self, name):
        return getattr(self.inner, name)

    async def append_events(self, run_id, events):
        self.writes.append("events")
        self.event_batches.append([event.type for event in events])
        return await self.inner.append_events(run_id, events)

    async def save_checkpoint(self, run_id, checkpoint, **kwargs):
        self.writes.append("checkpoint")
        return await self.inner.save_checkpoint(run_id, checkpoint, **kwargs)

    async def mark_completed(self, run_id, checkpoint):
        self.writes.append("completed")
        return await self.inner.mark_completed(run_id, checkpoint)

    @property
    def save_checkpoint_with_events(self):
        return self._atomic if self.atomic else None

    async def _atomic(self, run_id, checkpoint, events, *, prepare_checkpoint=None, **kwargs):
        def prepare(seq):
            if prepare_checkpoint is not None:
                prepare_checkpoint(seq)
            if self.fail_after_prepare:
                raise OSError("failed after binding checkpoint cursor")

        self.writes.append("atomic")
        self.atomic_phases.append(checkpoint.phase)
        return await self.inner.save_checkpoint_with_events(
            run_id, checkpoint, events, prepare_checkpoint=prepare, **kwargs
        )


async def _guard_runtime(pg_dsn, *, fail=False):
    from test_checkpoint_growth import _delta
    from test_run_resume import ScriptProvider, _agent, _memory_session_store

    from linch.loop.runtime import RunRuntime
    from linch.loop_guard import LoopGuard
    from linch.model_calls import _ModelCallLedger
    from linch.run_stores.postgres import PostgresRunStore

    store = _RecordingStore(PostgresRunStore(pg_dsn), fail_after_prepare=fail)
    agent = _agent(
        model="gpt-5",
        provider=ScriptProvider(),
        session_store=_memory_session_store(),
        run_store=store,
        cwd=".",
    )
    session = await agent.session()
    record = await store.create_run(session.id)
    runtime = RunRuntime(
        session=session,
        prompt="hi",
        opts=None,
        run_id=record.id,
        run_record=record,
        resume_checkpoint=None,
    )
    runtime.checkpoint = RunCheckpoint("tool_results_appended", "hi", 0, Usage())
    runtime.ledger = _ModelCallLedger(record.id)
    runtime.guard = LoopGuard()
    runtime.guard_evaluation_count = 1
    fact = _delta("batch")
    runtime.guard_evaluation_ids = {"batch"}
    runtime.guard_evaluation_facts = {"batch": fact}
    session._event_journal = RunEventBuffer(store, record.id, session=session)
    await store.save_checkpoint(record.id, runtime.checkpoint)
    await session._event_journal.append(fact)
    return agent, store, runtime, fact


async def test_atomic_turn_complete_binds_guard_to_actual_allocation_after_peer_append(pg_dsn):
    from linch.loop.checkpoint import (
        _loop_guard_runtime_from_extension,
        _loop_guard_state_from_events,
    )
    from linch.run_stores.postgres import PostgresRunStore

    agent, store, runtime, fact = await _guard_runtime(pg_dsn)
    peer = PostgresRunStore(pg_dsn)
    try:
        # The buffer's known cursor is stale; arithmetic prediction would bind to 1.
        await peer.append_event(runtime.run_id, PartialAssistantEvent(delta={"text": "peer"}))
        store.writes.clear()
        await runtime.save_checkpoint("turn_complete")
        assert store.writes == ["atomic"]
        record = await peer.load_run(runtime.run_id)
        assert _loop_guard_runtime_from_extension(record.checkpoint.extension_state) == (1, 2)
        assert (runtime.guard_committed_count, runtime.guard_evaluation_seq) == (1, 2)
        assert runtime.guard_evaluation_facts == {}
        assert runtime.guard_evaluation_ids == set()
        assert runtime.session._event_journal.last_seq == 2
        state = _loop_guard_state_from_events(
            runtime.guard,
            await peer.load_events(runtime.run_id),
            evaluation_count=1,
            evaluation_seq=2,
            current_batch_id=None,
        )
        assert state.call_counts == {'search:{"q":"x"}': 1}
    finally:
        await peer.close()
        await agent.close()


async def test_atomic_turn_complete_failure_preserves_live_guard_and_pending_facts(pg_dsn):
    from linch.loop.checkpoint import _loop_guard_runtime_from_extension

    agent, store, runtime, fact = await _guard_runtime(pg_dsn, fail=True)
    try:
        store.writes.clear()
        with pytest.raises(OSError, match="after binding"):
            await runtime.save_checkpoint("turn_complete")
        assert store.writes == ["atomic"]
        assert (runtime.guard_committed_count, runtime.guard_evaluation_seq) == (0, 0)
        assert _loop_guard_runtime_from_extension(runtime.checkpoint.extension_state) == (0, 0)
        assert runtime.guard_evaluation_facts == {"batch": fact}
        assert runtime.guard_evaluation_ids == {"batch"}
        assert runtime.session._event_journal._pending == [fact]
        assert runtime.session._event_journal.last_seq == 0
        assert await store.load_events(runtime.run_id) == []
        assert (await store.load_run(runtime.run_id)).checkpoint.phase == "tool_results_appended"
    finally:
        await agent.close()


async def test_phase_atomic_run_reduces_writes_and_keeps_tool_guard_durable(pg_dsn):
    import dataclasses
    import json
    import re

    from test_run_resume import CountingTool, ScriptProvider, _agent, _memory_session_store

    from linch.run_stores.postgres import PostgresRunStore
    from linch.tools import ToolRegistry

    traces, write_counts = [], []
    for atomic in (False, True):
        store = _RecordingStore(PostgresRunStore(pg_dsn), atomic=atomic)
        checked = []

        class DurableTool(CountingTool):
            async def execute(self, input, ctx, _store=store, _checked=checked):
                record = await _store.load_run(ctx.run_id)
                assert record.checkpoint.phase == "tool_executing"
                _checked.append(True)
                return await super().execute(input, ctx)

        tools = ToolRegistry()
        tools.register(DurableTool("A", {}))
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
            encoded = [dataclasses.asdict(event) for event in events]

            def scrub(value):
                if isinstance(value, dict):
                    return {
                        key: "<time>"
                        if key in {"duration_ms", "elapsed_ms", "timestamp"}
                        else scrub(item)
                        for key, item in value.items()
                    }
                if isinstance(value, list):
                    return [scrub(item) for item in value]
                if isinstance(value, str):
                    return re.sub(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", "<id>", value)
                return value

            traces.append(json.dumps(scrub(encoded), sort_keys=True))
            write_counts.append(len(store.writes))
            if atomic:
                assert "turn_complete" in store.atomic_phases
                assert "completed" in store.atomic_phases
                assert not {
                    "assistant_appended",
                    "tool_batch_pending",
                    "tool_executing",
                }.intersection(store.atomic_phases)
                for batch in store.event_batches:
                    if "tool_call_start" in batch or "tool_call_end" in batch:
                        assert len(batch) == 1
        finally:
            await agent.close()
    assert traces[0] == traces[1]
    assert write_counts[1] < write_counts[0]


async def test_forwarded_bound_atomic_capability_preserves_adapter_event_override() -> None:
    inner = _AtomicBufferStore(None)
    observed = []

    class Adapter:
        def __getattr__(self, name):
            return getattr(inner, name)

        async def append_events(self, run_id, events):
            observed.extend(events)
            return [5, 6]

    buffer = RunEventBuffer(Adapter(), "run", initial_seq=4)
    inner.buffer = buffer
    events = [PartialAssistantEvent(delta={"text": text}) for text in ("a", "b")]
    for event in events:
        await buffer.append(event)
    assert not buffer.can_flush_with_checkpoint
    assert not await buffer.flush_with_checkpoint(RunCheckpoint("turn_complete", "hi", 0, Usage()))
    assert await buffer.flush() == 6
    assert observed == events
    assert inner.saved == []
