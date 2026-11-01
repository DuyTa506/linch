from __future__ import annotations

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


class _TrackingBatchStore(InMemoryRunStore):
    def __init__(self) -> None:
        super().__init__()
        self.leases: list[Any] = []

    async def append_events(
        self, run_id: str, events: list[Any], *, lease: RunLease | None = None
    ) -> list[int]:
        self.leases.append(lease)
        return await super().append_events(run_id, events, lease=lease)


class _NoLeaseCreateStore(InMemoryRunStore):
    def __init__(self) -> None:
        super().__init__()
        self.create_calls = 0

    async def create_run(
        self,
        session_id: str,
        *,
        id: str | None = None,
        meta: dict[str, object] | None = None,
        initial_checkpoint: Any = None,
    ) -> Any:
        self.create_calls += 1
        return await super().create_run(
            session_id,
            id=id,
            meta=meta,
            initial_checkpoint=initial_checkpoint,
        )


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


class _OpaqueCallable:
    def __init__(self, callback: Any) -> None:
        self._callback = callback

    @property
    def __signature__(self) -> Any:
        raise ValueError("opaque callable")

    async def __call__(self, run_id: str, event: Any) -> int:
        return await self._callback(run_id, event)


class _OpaqueAppendStore(InMemoryRunStore):
    def __init__(self) -> None:
        super().__init__()
        self.create_calls = 0
        self.append_calls = 0
        self.append_events = None
        self.append_event = _OpaqueCallable(self._append_event)

    async def create_run(self, session_id: str, **kwargs: Any) -> Any:
        self.create_calls += 1
        return await super().create_run(session_id, **kwargs)

    async def _append_event(self, run_id: str, event: Any) -> int:
        self.append_calls += 1
        return await InMemoryRunStore.append_event(self, run_id, event)


class _VarPositionalCallable:
    def __init__(self, callback: Any) -> None:
        self._callback = callback

    async def __call__(self, run_id: str, event: Any, *lease: Any) -> int:
        return await self._callback(run_id, event)


class _VarPositionalAppendStore(InMemoryRunStore):
    def __init__(self) -> None:
        super().__init__()
        self.create_calls = 0
        self.append_events = None
        self.append_event = _VarPositionalCallable(self._append_event)

    async def create_run(self, session_id: str, **kwargs: Any) -> Any:
        self.create_calls += 1
        return await super().create_run(session_id, **kwargs)

    async def _append_event(self, run_id: str, event: Any) -> int:
        return await InMemoryRunStore.append_event(self, run_id, event)


class _FailOnceBatchStore:
    def __init__(self) -> None:
        self.fail = True
        self.events: list[Any] = []

    async def append_events(self, run_id: str, events: list[Any]) -> list[int]:
        if self.fail:
            self.fail = False
            raise OSError("temporary batch failure")
        self.events.extend(events)
        return list(range(1, len(events) + 1))


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


async def test_failed_batch_flush_restores_pending_events_for_retry() -> None:
    store = _FailOnceBatchStore()
    buffer = RunEventBuffer(store, "r1")
    events = [PartialAssistantEvent(delta={"text": text}) for text in ("a", "b")]
    for event in events:
        await buffer.append(event)

    await _expect_flush_failure(buffer)

    assert buffer.last_seq == 0
    assert buffer._pending == events
    assert await buffer.flush() == 2
    assert store.events == events


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


async def test_event_buffer_forwards_captured_lease_to_batch_writes() -> None:
    store = _TrackingBatchStore()
    record = await store.create_run("s1", id="r1")
    lease = await store.claim(record.id, "worker-a", 30.0)
    buffer = RunEventBuffer(store, record.id, lease=lease)
    event = PartialAssistantEvent(delta={"text": "leased"})

    await buffer.append(event)
    await buffer.flush()

    rows = await store.load_events(record.id)
    assert [row.event for row in rows] == [event]
    assert store.leases == [lease]


async def test_event_buffer_rejects_unsupported_leased_batch_before_side_effect() -> None:
    store = _RecordingRunStore()
    buffer = RunEventBuffer(store, "r1", lease=RunLease("r1", "owner", 1, 999.0, 30.0))
    event = PartialAssistantEvent(delta={"text": "leased"})
    await buffer.append(event)

    with pytest.raises(TypeError, match="lease"):
        await buffer.flush()
    assert store.batches == []


async def test_session_rejects_unsupported_leased_store_before_create() -> None:
    from linch.session import RunOptions

    store = _NoLeaseCreateStore()
    agent = make_agent(
        ScriptedProvider([TextTurn("done")]),
        model="gpt-5",
        session_store=memory_session_store(),
        run_store=store,
    )
    session = await agent.session(id="s1")
    lease = RunLease("r1", "owner", 1, 999.0, 30.0)

    with pytest.raises(TypeError, match="lease"):
        session.run("hello", RunOptions(run_id="r1", lease=lease))
    assert store.create_calls == 0


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


async def test_session_rejects_opaque_leased_store_before_create() -> None:
    from linch.session import RunOptions

    provider = ScriptedProvider([TextTurn("must-not-run")])
    store = _OpaqueAppendStore()
    agent = make_agent(
        provider,
        model="gpt-5",
        session_store=memory_session_store(),
        run_store=store,
    )
    session = await agent.session(id="s1")
    record = await store.create_run(session.id, id="opaque-run")
    store._runs[record.id].status = "pending"
    lease = await store.claim(record.id, "worker-a", 30.0)
    before_create = store.create_calls

    try:
        with pytest.raises(TypeError, match="does not support lease"):
            iterator = session.run("hello", RunOptions(run_id=record.id, lease=lease))
            _ = [event async for event in iterator]
        assert store.create_calls == before_create
        assert store.append_calls == 0
        assert provider.requests == []
        assert await store.load_events(record.id) == []
    finally:
        await agent.close()


async def test_unleased_opaque_store_keeps_old_call_shape() -> None:
    provider = ScriptedProvider([TextTurn("done")])
    store = _OpaqueAppendStore()
    agent = make_agent(
        provider,
        model="gpt-5",
        session_store=memory_session_store(),
        run_store=store,
    )

    try:
        session = await agent.session(id="s1")
        events = [event async for event in session.run("hello")]
        assert events[-1].subtype == "success"
        assert store.append_calls > 0
    finally:
        await agent.close()


async def test_session_rejects_var_positional_lease_before_create() -> None:
    from linch.session import RunOptions

    provider = ScriptedProvider([TextTurn("must-not-run")])
    store = _VarPositionalAppendStore()
    agent = make_agent(
        provider,
        model="gpt-5",
        session_store=memory_session_store(),
        run_store=store,
    )
    session = await agent.session(id="s1")
    record = await store.create_run(session.id, id="var-positional-run")
    store._runs[record.id].status = "pending"
    lease = await store.claim(record.id, "worker-a", 30.0)
    before_create = store.create_calls

    try:
        with pytest.raises(TypeError, match="does not support lease"):
            iterator = session.run("hello", RunOptions(run_id=record.id, lease=lease))
            _ = [event async for event in iterator]
        assert store.create_calls == before_create
        assert provider.requests == []
    finally:
        await agent.close()


def test_lease_signature_accepts_keyword_and_kwargs_parameters() -> None:
    from linch.run_store import _supports_lease_keyword

    async def keyword_only(run_id: str, event: Any, *, lease: Any = None) -> int:
        return 1

    async def keyword_or_positional(run_id: str, event: Any, lease: Any = None) -> int:
        return 1

    async def arbitrary_keywords(run_id: str, event: Any, **kwargs: Any) -> int:
        return 1

    assert _supports_lease_keyword(keyword_only)
    assert _supports_lease_keyword(keyword_or_positional)
    assert _supports_lease_keyword(arbitrary_keywords)


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

    with pytest.raises(LeaseLostError):
        await runtime.save_checkpoint("turn_complete")

    assert new_buffer._pending == [event]
    assert await store.load_events(record.id) == []


async def test_observational_events_batch_and_tool_events_flush_per_event() -> None:
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
        for etype in ("tool_call_start", "tool_call_end"):
            if etype in batch:
                assert batch == [etype], f"{etype} must flush alone, got {batch}"

    assert any(len(batch) > 1 for batch in run_store.batches), run_store.batches
