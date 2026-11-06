from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import pytest

from linch.events import PartialAssistantEvent
from linch.loop.checkpoint import RunEventBuffer, _persist_event
from linch.loop.terminals import _complete_with_pending_events
from linch.run_store import InMemoryRunStore, LeaseLostError, RunCheckpoint, RunLease
from linch.types import Usage
from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn, ToolUseTurn
from tests.support.stores import memory_session_store
from tests.support.tools import StubTool, tool_registry


class _RecordingRunStore(InMemoryRunStore):
    def __init__(self) -> None:
        super().__init__()
        self.batches: list[list[str]] = []

    async def append_events(self, run_id: str, events: list[Any]) -> list[int]:
        self.batches.append([event.type for event in events])
        return await super().append_events(run_id, events)

    async def save_checkpoint_with_events(
        self, run_id: str, checkpoint: Any, events: list[Any], *, status: str = "running"
    ) -> tuple[Any, list[int]]:
        self.batches.append([event.type for event in events])
        return await super().save_checkpoint_with_events(run_id, checkpoint, events, status=status)


class _NoLeaseSnapshotStore(InMemoryRunStore):
    def __init__(self) -> None:
        super().__init__()
        self.create_calls = 0
        self.save_calls = 0

    async def create_run(
        self,
        session_id: str,
        *,
        id: str | None = None,
        meta: dict[str, object] | None = None,
        initial_checkpoint: Any = None,
        lease: RunLease | None = None,
    ) -> Any:
        self.create_calls += 1
        return await super().create_run(
            session_id,
            id=id,
            meta=meta,
            initial_checkpoint=initial_checkpoint,
            lease=lease,
        )

    async def save(self, run_id: str, provider_attempt: int, request: Any) -> Any:
        self.save_calls += 1
        return await super().save(run_id, provider_attempt, request)


class _InterleavingRunStore(InMemoryRunStore):
    """Run a queued hook inside an atomic write, where Postgres lets other tasks run."""

    def __init__(self) -> None:
        super().__init__()
        self.during_write: list[Callable[[], Awaitable[None]]] = []

    async def save_checkpoint_with_events(
        self,
        run_id: str,
        checkpoint: RunCheckpoint,
        events: Any,
        *,
        status: str = "running",
        lease: RunLease | None = None,
    ) -> list[int]:
        if self.during_write:
            await self.during_write.pop(0)()
        return await super().save_checkpoint_with_events(
            run_id, checkpoint, events, status=status, lease=lease
        )


class _StatusRecordingRunStore(InMemoryRunStore):
    def __init__(self) -> None:
        super().__init__()
        self.writes: list[tuple[str, list[Any]]] = []

    async def append_events(
        self, run_id: str, events: list[Any], *, lease: RunLease | None = None
    ) -> list[int]:
        self.writes.append(("running", list(events)))
        return await super().append_events(run_id, events, lease=lease)

    async def save_checkpoint_with_events(
        self,
        run_id: str,
        checkpoint: RunCheckpoint,
        events: Any,
        *,
        status: str = "running",
        lease: RunLease | None = None,
    ) -> list[int]:
        self.writes.append((status, list(events)))
        return await super().save_checkpoint_with_events(
            run_id, checkpoint, events, status=status, lease=lease
        )


class _FailSecondSingleStore:
    def __init__(self) -> None:
        self.calls = 0
        self.events: list[Any] = []
        self.buffer: RunEventBuffer | None = None
        self.concurrent_event = PartialAssistantEvent(delta={"text": "concurrent"})

    async def append_event(self, run_id: str, event: Any) -> int:
        self.calls += 1
        if self.calls == 2:
            assert self.buffer is not None
            await self.buffer.append(self.concurrent_event)
            raise OSError("temporary append failure")
        self.events.append(event)
        return len(self.events)


async def _expect_flush_failure(buffer: RunEventBuffer) -> None:
    with pytest.raises(OSError, match="temporary"):
        await buffer.flush()


async def test_failed_single_event_flush_preserves_progress_and_requeues_unattempted() -> None:
    store = _FailSecondSingleStore()
    buffer = RunEventBuffer(store, "r1")
    store.buffer = buffer
    events = [PartialAssistantEvent(delta={"text": text}) for text in ("a", "b", "c")]
    for event in events:
        await buffer.append(event)

    await _expect_flush_failure(buffer)

    assert buffer.last_seq == 1
    assert buffer._pending == [events[1], events[2], store.concurrent_event]
    assert store.events == [events[0]]

    assert await buffer.flush() == 4
    assert store.events == [*events, store.concurrent_event]


async def test_event_buffer_rejects_unsupported_leased_batch_before_side_effect() -> None:
    store = _RecordingRunStore()
    buffer = RunEventBuffer(store, "r1", lease=RunLease("r1", "owner", 1, 999.0, 30.0))
    event = PartialAssistantEvent(delta={"text": "leased"})
    await buffer.append(event)

    with pytest.raises(TypeError, match="lease"):
        await buffer.flush()
    assert store.batches == []


async def test_session_rejects_unsupported_leased_snapshot_before_create() -> None:
    from linch import DurabilityOptions
    from linch.session import RunOptions

    store = _NoLeaseSnapshotStore()
    agent = make_agent(
        ScriptedProvider([TextTurn("done")]),
        model="gpt-5",
        session_store=memory_session_store(),
        run_store=store,
        durability=DurabilityOptions(exact_model_input=True),
    )
    session = await agent.session(id="s1")
    lease = RunLease("r1", "owner", 1, 999.0, 30.0)

    with pytest.raises(TypeError, match="lease"):
        session.run("hello", RunOptions(run_id="r1", lease=lease))
    assert store.create_calls == store.save_calls == 0


async def test_explicit_lease_does_not_borrow_newer_event_buffer() -> None:
    from linch import Context

    store = InMemoryRunStore()
    record = await store.create_run("s1", id="r1")
    old_lease = await store.claim(record.id, "worker-a", 30.0)
    await store.release(old_lease)
    new_lease = await store.claim(record.id, "worker-b", 30.0)
    new_buffer = RunEventBuffer(store, record.id, lease=new_lease)
    session = SimpleNamespace(
        _event_journal=new_buffer,
        agent=SimpleNamespace(run_store=store, context=Context()),
        active_run_id=None,
    )

    with pytest.raises(LeaseLostError):
        await _persist_event(
            session,
            record.id,
            PartialAssistantEvent(delta={"text": "stale"}),
            lease=old_lease,
        )

    assert new_buffer._pending == []
    assert await store.load_events(record.id) == []


async def test_terminal_explicit_lease_does_not_flush_newer_event_buffer() -> None:
    from linch import Context

    store = InMemoryRunStore()
    record = await store.create_run("s1", id="r1")
    old_lease = await store.claim(record.id, "worker-a", 30.0)
    await store.release(old_lease)
    new_lease = await store.claim(record.id, "worker-b", 30.0)
    new_buffer = RunEventBuffer(store, record.id, lease=new_lease)
    event = PartialAssistantEvent(delta={"text": "stale"})
    await new_buffer.append(event)
    session = SimpleNamespace(
        _event_journal=new_buffer,
        agent=SimpleNamespace(run_store=store, context=Context()),
        active_run_id=None,
    )
    checkpoint = RunCheckpoint("started", "hello", 0, Usage())

    with pytest.raises(LeaseLostError):
        await _complete_with_pending_events(session, record.id, checkpoint, lease=old_lease)

    assert new_buffer._pending == [event]
    assert await store.load_events(record.id) == []


async def test_runtime_failed_status_does_not_flush_newer_event_buffer() -> None:
    from linch import Context
    from linch.loop.runtime import RunRuntime

    store = InMemoryRunStore()
    record = await store.create_run("s1", id="r1")
    old_lease = await store.claim(record.id, "worker-a", 30.0)
    await store.release(old_lease)
    new_lease = await store.claim(record.id, "worker-b", 30.0)
    new_buffer = RunEventBuffer(store, record.id, lease=new_lease)
    await new_buffer.append(PartialAssistantEvent(delta={"text": "stale"}))
    agent = SimpleNamespace(run_store=store, context=Context())
    session = SimpleNamespace(
        _event_journal=new_buffer,
        agent=agent,
        active_run_id=None,
        workers={},
        background_tasks=[],
    )
    runtime = RunRuntime.__new__(RunRuntime)
    runtime.agent = agent
    runtime.session = session
    runtime.run_record = record
    runtime.run_id = record.id
    runtime.lease = old_lease
    runtime.event_buffer = new_buffer
    runtime.checkpoint = RunCheckpoint("started", "hello", 0, Usage())
    runtime.exact_model_input = False
    runtime.total = Usage()
    runtime.lease_lost = None
    runtime.sync_checkpoint_accounting = lambda: None

    failure = await runtime.persist_failed({"name": "stale", "message": "old"})

    assert isinstance(failure, LeaseLostError)
    assert session._event_journal is new_buffer
    assert new_buffer._pending
    assert await store.load_events(record.id) == []


async def test_runtime_checkpoint_does_not_borrow_newer_event_buffer() -> None:
    from linch import Context
    from linch.loop.runtime import RunRuntime
    from linch.tools.file_tracker import FileReadTracker

    store = InMemoryRunStore()
    record = await store.create_run("s1", id="r1")
    old_lease = await store.claim(record.id, "worker-a", 30.0)
    await store.release(old_lease)
    new_lease = await store.claim(record.id, "worker-b", 30.0)
    old_buffer = RunEventBuffer(store, record.id, lease=old_lease)
    new_buffer = RunEventBuffer(store, record.id, lease=new_lease)
    event = PartialAssistantEvent(delta={"text": "newer"})
    await new_buffer.append(event)
    agent = SimpleNamespace(run_store=store, context=Context())
    session = SimpleNamespace(
        _event_journal=new_buffer,
        _extensions={},
        file_read_tracker=FileReadTracker(),
        active_model=None,
        fallback_index=0,
        pending_skill_overlay=None,
        current_turn_allowed_tools=None,
        current_turn_permission_decisions={},
        workers={},
        alignment_queue=[],
        agent=agent,
        active_run_id=None,
    )
    runtime = RunRuntime.__new__(RunRuntime)
    runtime.agent = agent
    runtime.session = session
    runtime.run_record = record
    runtime.run_id = record.id
    runtime.lease = old_lease
    runtime.event_buffer = old_buffer
    runtime.checkpoint = RunCheckpoint("started", "hello", 0, Usage())
    runtime.exact_model_input = False
    runtime.total = Usage()
    runtime.active_primary = None
    runtime.force_final_pending = False
    runtime.guard_state = None
    runtime.checkpointable_hooks = {}
    runtime.reliability = None
    runtime.truncation_attempts = 0
    runtime.truncation_prefix = ""
    runtime.pending_truncation_feedback = None
    runtime.sync_checkpoint_accounting = lambda: None

    # A running checkpoint is held for the next barrier, where the stale lease fails.
    await runtime.save_checkpoint("turn_complete")
    with pytest.raises(LeaseLostError):
        await old_buffer.flush()

    assert new_buffer._pending == [event]
    assert await store.load_events(record.id) == []


async def test_events_group_commit_and_tool_start_closes_its_batch() -> None:
    session_store = memory_session_store()
    run_store = _RecordingRunStore()
    agent = make_agent(
        ScriptedProvider([ToolUseTurn("A", {}), TextTurn("done")]),
        model="gpt-5",
        session_store=session_store,
        run_store=run_store,
        tools=tool_registry(StubTool("A")),
    )
    session = await agent.session(id="s1")

    run_id = ""
    async for event in session.run("hello"):
        if event.type == "system":
            run_id = event.run_id
    assert run_id

    flat = [t for batch in run_store.batches for t in batch]
    durable = [row.event.type for row in await run_store.load_events(run_id)]
    assert flat == durable
    assert "tool_call_start" in durable and "tool_call_end" in durable

    for batch in run_store.batches:
        if "tool_call_start" in batch:
            # A tool start is a barrier: it is the last record before the tool runs.
            assert batch[-1] == "tool_call_start", batch

    assert any(len(batch) > 1 for batch in run_store.batches), run_store.batches


async def test_checkpoint_deferred_during_a_session_barrier_stays_held() -> None:
    store = _InterleavingRunStore()
    record = await store.create_run("s1", id="r1")
    buffer = RunEventBuffer(store, record.id)
    await buffer.append(PartialAssistantEvent(delta={"text": "a"}))
    buffer.defer_checkpoint(RunCheckpoint("assistant_appended", "hi", 0, Usage()))

    async def loop_saves_newer_phase() -> None:
        buffer.defer_checkpoint(RunCheckpoint("tool_results_appended", "hi", 0, Usage()))

    store.during_write.append(loop_saves_newer_phase)

    await buffer.flush_checkpoint()  # a background notify's session-store barrier
    await buffer.flush()  # the loop's next barrier

    run = await store.load_run(record.id)
    assert run is not None and run.checkpoint is not None
    assert run.checkpoint.phase == "tool_results_appended"


async def test_overlapping_barriers_commit_in_buffer_order() -> None:
    store = _InterleavingRunStore()
    record = await store.create_run("s1", id="r1")
    buffer = RunEventBuffer(store, record.id)
    first = PartialAssistantEvent(delta={"text": "first"})
    second = PartialAssistantEvent(delta={"text": "second"})
    await buffer.append(first)
    buffer.defer_checkpoint(RunCheckpoint("assistant_appended", "hi", 0, Usage()))
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold_open() -> None:
        entered.set()
        await release.wait()

    store.during_write.append(hold_open)

    background = asyncio.create_task(buffer.flush_checkpoint())
    await entered.wait()
    await buffer.append(second)
    buffer.defer_checkpoint(RunCheckpoint("tool_results_appended", "hi", 0, Usage()))
    loop_barrier = asyncio.create_task(buffer.flush())
    await asyncio.sleep(0)  # an unserialized write would commit here, ahead of the first
    release.set()
    await asyncio.gather(background, loop_barrier)

    run = await store.load_run(record.id)
    assert run is not None and run.checkpoint is not None
    assert run.checkpoint.phase == "tool_results_appended"
    assert [row.event for row in await store.load_events(record.id)] == [first, second]


async def test_session_barrier_keeps_uncovered_events_ahead_of_later_appends() -> None:
    store = _InterleavingRunStore()
    record = await store.create_run("s1", id="r1")
    buffer = RunEventBuffer(store, record.id)
    covered, uncovered, appended = (
        PartialAssistantEvent(delta={"text": text}) for text in ("covered", "uncovered", "appended")
    )
    await buffer.append(covered)
    buffer.defer_checkpoint(RunCheckpoint("assistant_appended", "hi", 0, Usage()))
    await buffer.append(uncovered)

    async def loop_appends() -> None:
        await buffer.append(appended)

    store.during_write.append(loop_appends)

    await buffer.flush_checkpoint()  # commits only the event the held checkpoint covers
    assert buffer._pending == [uncovered, appended]
    await buffer.flush()

    rows = await store.load_events(record.id)
    assert [row.event for row in rows] == [covered, uncovered, appended]
    assert [row.seq for row in rows] == [1, 2, 3]


@pytest.mark.parametrize("barrier", [False, True])
async def test_terminal_cleanup_cannot_commit_the_result_as_running(barrier: bool) -> None:
    from linch import Context
    from linch.events import ResultEvent

    store = _StatusRecordingRunStore()
    record = await store.create_run("s1", id="r1")
    buffer = RunEventBuffer(store, record.id)
    late = PartialAssistantEvent(delta={"text": "late"})

    class _Processes:
        async def close(self) -> None:
            # A model call left running past its compaction records here.
            await _persist_event(session, record.id, late)
            if barrier:
                await buffer.flush()  # its dispatch marker or receipt is a barrier

    session = SimpleNamespace(
        _event_journal=buffer,
        _processes=_Processes(),
        agent=SimpleNamespace(run_store=store, context=Context()),
        active_run_id=None,
    )
    await buffer.append(PartialAssistantEvent(delta={"text": "turn"}))
    buffer.defer_checkpoint(RunCheckpoint("turn_complete", "hi", 0, Usage()))
    result = ResultEvent(
        subtype="success", stop_reason="end_turn", total_usage=Usage(), duration_ms=0
    )
    await _persist_event(session, record.id, result)

    await _complete_with_pending_events(
        session, record.id, RunCheckpoint("turn_complete", "hi", 0, Usage())
    )

    assert all(
        event is not result
        for status, events in store.writes
        if status == "running"
        for event in events
    )
    status, events = store.writes[-1]
    assert status == "completed" and events[-1] is result
