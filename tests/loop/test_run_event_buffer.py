"""Per-run event buffering (WS2, safe/observational-only design).

Observational events are coalesced into batched `append_events` flushes;
recovery-critical tool start/end events are flushed one-per-event so they stay
durable at the yield boundary. Consumer-visible order and log completeness are
unchanged.
"""

from __future__ import annotations

from typing import Any

from test_run_resume import ScriptProvider, _agent, _memory_session_store, _registry

from linch.events import PartialAssistantEvent
from linch.loop.checkpoint import RunEventBuffer
from linch.run_store import InMemoryRunStore


class _RecordingRunStore(InMemoryRunStore):
    """Records the event-type list of every append_events flush."""

    def __init__(self) -> None:
        super().__init__()
        self.batches: list[list[str]] = []

    async def append_events(self, run_id: str, events: list[Any]) -> list[int]:
        self.batches.append([event.type for event in events])
        return await super().append_events(run_id, events)


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


async def test_failed_batch_flush_restores_pending_events_for_retry() -> None:
    store = _FailOnceBatchStore()
    buffer = RunEventBuffer(store, "r1")
    events = [PartialAssistantEvent(delta={"text": text}) for text in ("a", "b")]
    for event in events:
        await buffer.append(event)

    try:
        await buffer.flush()
        raise AssertionError("expected first flush to fail")
    except OSError:
        pass

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

    try:
        await buffer.flush()
        raise AssertionError("expected first flush to fail")
    except OSError:
        pass

    assert buffer.last_seq == 1
    assert buffer._pending == [events[1], events[2], store.concurrent_event]
    assert store.events == [events[0]]

    assert await buffer.flush() == 4
    assert store.events == [*events, store.concurrent_event]


async def test_observational_events_batch_and_tool_events_flush_per_event() -> None:
    session_store = _memory_session_store()
    run_store = _RecordingRunStore()
    counts: dict[str, int] = {}
    agent = _agent(
        model="gpt-5",
        provider=ScriptProvider(tool_names=["A"]),
        session_store=session_store,
        run_store=run_store,
        tools=_registry(counts, "A"),
        cwd=".",
    )
    session = await agent.session(id="s1")

    run_id = ""
    async for event in session.run("hello"):
        if event.type == "system":
            run_id = event.run_id
    assert run_id

    # Completeness + order: the flushed batches, concatenated, equal the durable
    # log exactly (nothing lost, nothing reordered).
    flat = [t for batch in run_store.batches for t in batch]
    durable = [row.event.type for row in await run_store.load_events(run_id)]
    assert flat == durable
    assert "tool_call_start" in durable and "tool_call_end" in durable

    # Recovery-critical events are flushed one-per-event (durable at the yield).
    for batch in run_store.batches:
        for etype in ("tool_call_start", "tool_call_end"):
            if etype in batch:
                assert batch == [etype], f"{etype} must flush alone, got {batch}"

    # Observational coalescing actually happened: at least one multi-event flush.
    assert any(len(batch) > 1 for batch in run_store.batches), run_store.batches
