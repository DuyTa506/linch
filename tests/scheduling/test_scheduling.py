"""Scheduling primitive (ROADMAP Phase 3.3).

A neutral time-trigger: cron/interval ``Schedule``s in a ``ScheduleStore``, an
async ``SchedulerLoop`` that fires due schedules into ``session.pending_notifications``
(reusing the background drain) and emits a ``ScheduleEvent``, plus auto-registered
create/list/cancel tools. The firing payload/policy is the embedder's.

Verify: a ``* * * * *`` schedule fires once/minute as a UserEvent; durable
schedules survive a store reload; an invalid expression is rejected at register
time.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast

import pytest

# ── cron utility ─────────────────────────────────────────────────────────────


def test_validate_cron_accepts_and_rejects() -> None:
    from linch import validate_cron

    for ok in ("* * * * *", "*/5 * * * *", "0 9 * * 1-5", "30 0,12 1 1 *"):
        assert validate_cron(ok) == ok
    for bad in ("* * * *", "60 * * * *", "* 24 * * *", "* * * * 8", "*/0 * * * *", "a * * * *"):
        with pytest.raises(ValueError):
            validate_cron(bad)


def test_cron_matches_every_minute_and_business_hours() -> None:
    from linch import cron_matches

    dt = datetime(2026, 6, 12, 14, 37, tzinfo=timezone.utc)  # a Friday
    assert cron_matches("* * * * *", dt)
    assert cron_matches("37 14 * * *", dt)
    assert not cron_matches("38 14 * * *", dt)
    # Friday = cron weekday 5; Mon-Fri matches, Sat/Sun would not.
    assert cron_matches("* * * * 1-5", dt)
    saturday = datetime(2026, 6, 13, 14, 37, tzinfo=timezone.utc)
    assert not cron_matches("* * * * 1-5", saturday)


def test_next_cron_time_advances_one_minute() -> None:
    from linch import next_cron_time

    base = datetime(2026, 6, 12, 14, 37, 30, tzinfo=timezone.utc).timestamp()
    nxt = next_cron_time("* * * * *", base)
    nxt_dt = datetime.fromtimestamp(nxt, tz=timezone.utc)
    assert (nxt_dt.hour, nxt_dt.minute, nxt_dt.second) == (14, 38, 0)


# ── Schedule model ───────────────────────────────────────────────────────────


def test_schedule_requires_exactly_one_trigger() -> None:
    from linch import Schedule

    with pytest.raises(ValueError):
        Schedule(payload="x")  # neither
    with pytest.raises(ValueError):
        Schedule(payload="x", cron="* * * * *", interval_s=60)  # both
    with pytest.raises(ValueError):
        Schedule(payload="x", cron="bad expr")
    s = Schedule(payload="x", interval_s=30)
    assert s.compute_next_run(1000.0) == 1030.0


# ── SchedulerLoop firing ─────────────────────────────────────────────────────


class _FakeSession:
    def __init__(self) -> None:
        self.pending_notifications: list[Any] = []


class _DurableSession:
    def __init__(self) -> None:
        self.id = "durable-session"
        self.agent = SimpleNamespace(
            durability=SimpleNamespace(durable_inbox=True, delivery_lease_s=10.0)
        )
        self.notifications: list[dict[str, Any]] = []
        self.fail: BaseException | None = None
        self.order: list[str] = []

    async def notify(self, message: Any, **kwargs: Any) -> str:
        self.order.append("notify")
        if self.fail is not None:
            raise self.fail
        self.notifications.append({"message": message, **kwargs})
        return str(kwargs["delivery_id"])


async def test_loop_fires_due_schedule_as_pending_notification() -> None:
    from linch import InMemoryScheduleStore, Schedule, SchedulerLoop
    from linch.events import ScheduleEvent

    now = [1000.0]
    store = InMemoryScheduleStore()
    schedule = Schedule(payload="run nightly report", cron="* * * * *")
    schedule.next_run = schedule.compute_next_run(now[0])  # ~1 minute out
    await store.add(schedule)

    session = _FakeSession()
    events: list[ScheduleEvent] = []
    loop = SchedulerLoop(store, session, clock=lambda: now[0], on_event=events.append)

    # Not due yet.
    assert await loop.tick() == []
    assert session.pending_notifications == []

    # Advance past the next run → it fires exactly once.
    now[0] += 61
    fired = await loop.tick()
    assert [s.id for s in fired] == [schedule.id]
    assert len(session.pending_notifications) == 1
    msg = session.pending_notifications[0]
    assert "scheduled-task" in msg.content[0].text
    assert "run nightly report" in msg.content[0].text
    assert [e.status for e in events] == ["fired"]

    # A second tick at the same time does not double-fire (next_run advanced).
    assert await loop.tick() == []
    assert len(session.pending_notifications) == 1


async def test_loop_skips_disabled_schedule() -> None:
    from linch import InMemoryScheduleStore, Schedule, SchedulerLoop

    store = InMemoryScheduleStore()
    schedule = Schedule(payload="x", interval_s=10, next_run=500.0, enabled=False)
    await store.add(schedule)
    session = _FakeSession()
    loop = SchedulerLoop(store, session, clock=lambda: 1000.0)

    assert await loop.tick() == []
    assert session.pending_notifications == []


async def test_durable_loop_claims_notifies_acks_then_emits() -> None:
    from linch.coordination.scheduling import InMemoryScheduleStore, Schedule, SchedulerLoop

    session = _DurableSession()

    class RecordingStore(InMemoryScheduleStore):
        async def ack_occurrences(self, occurrences: Any) -> None:
            session.order.append("ack")
            await super().ack_occurrences(occurrences)

    store = RecordingStore()
    await store.add(
        Schedule(
            id="nightly",
            payload="run report",
            interval_s=60,
            next_run=1000.0,
            metadata={"tenant": "acme"},
        )
    )

    def on_event(event: Any) -> None:
        session.order.append("event")
        assert event.schedule_id == "nightly"

    loop = SchedulerLoop(store, session, clock=lambda: 1000.0, on_event=on_event)
    fired = await loop.tick()

    assert len(fired) == 1
    occurrence = fired[0]
    notification = session.notifications[0]
    assert notification["delivery_id"] == f"schedule:{occurrence.id}"
    assert notification["source"] == "schedule"
    assert notification["metadata"] == {
        "schedule_id": "nightly",
        "occurrence_id": occurrence.id,
    }
    assert notification["message"].role == "user"
    assert "run report" in notification["message"].content[0].text
    assert session.order == ["notify", "ack", "event"]
    assert await store.claim_due_occurrences(1001.0, owner="probe", lease_s=10) == []


async def test_durable_loop_releases_failed_delivery_for_same_id_retry() -> None:
    from linch.coordination.scheduling import InMemoryScheduleStore, Schedule, SchedulerLoop

    store = InMemoryScheduleStore()
    await store.add(Schedule(id="retry", payload="p", interval_s=60, next_run=1000.0))
    session = _DurableSession()
    session.fail = RuntimeError("inbox unavailable")
    loop = SchedulerLoop(store, session, clock=lambda: 1000.0)

    assert await loop.tick() == []
    session.fail = None
    delivered = await loop.tick()

    assert len(delivered) == 1
    assert session.notifications[0]["delivery_id"] == f"schedule:{delivered[0].id}"


async def test_durable_loop_releases_on_cancellation_and_propagates() -> None:
    from linch.coordination.scheduling import InMemoryScheduleStore, Schedule, SchedulerLoop

    store = InMemoryScheduleStore()
    await store.add(Schedule(id="cancel", payload="p", interval_s=60, next_run=1000.0))

    class SlowSession(_DurableSession):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.block = True

        async def notify(self, message: Any, **kwargs: Any) -> str:
            if self.block:
                self.started.set()
                await asyncio.Future()
            return await super().notify(message, **kwargs)

    session = SlowSession()
    loop = SchedulerLoop(store, session, clock=lambda: 1000.0)

    tick = asyncio.create_task(loop.tick())
    await session.started.wait()
    tick.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tick

    session.block = False
    assert len(await loop.tick()) == 1


async def test_durable_loop_requires_leased_store_before_legacy_claim() -> None:
    from linch.coordination.scheduling import Schedule, SchedulerLoop
    from linch.errors import ConfigError

    schedule = Schedule(id="legacy", payload="p", interval_s=60, next_run=1000.0)

    class LegacyClaimingStore:
        def __init__(self) -> None:
            self.claim_calls = 0

        async def add(self, item: Any) -> None:
            pass

        async def update(self, item: Any) -> None:
            pass

        async def remove(self, schedule_id: str) -> bool:
            return False

        async def get(self, schedule_id: str) -> Any:
            return schedule

        async def list(self) -> list[Any]:
            return [schedule]

        async def claim_due(self, now: float) -> list[Any]:
            self.claim_calls += 1
            return [schedule]

    store = LegacyClaimingStore()
    loop = SchedulerLoop(cast(Any, store), _DurableSession(), clock=lambda: 1000.0)

    with pytest.raises(ConfigError, match="LeasedScheduleStore"):
        await loop.tick()
    assert store.claim_calls == 0
    assert schedule.next_run == 1000.0


async def test_fired_schedule_drains_as_user_event() -> None:
    # End-to-end: a fired schedule surfaces as a UserEvent on the next run, via
    # the same pending_notifications drain background workers use.
    from linch import Agent, InMemoryScheduleStore, Schedule, SchedulerLoop
    from linch.evals import ScriptedProvider, TextTurn
    from linch.sessions import InMemorySessionStore

    store = InMemoryScheduleStore()
    agent = Agent(
        model="m",
        provider=ScriptedProvider([TextTurn(text="ok")]),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        cwd=".",
        schedule_store=store,
    )
    session = await agent.session()

    schedule = Schedule(payload="ping", interval_s=10, next_run=0.0)
    await store.add(schedule)
    loop = SchedulerLoop(store, session, clock=lambda: 1000.0)
    await loop.tick()

    events = [event async for event in session.run("go")]
    user_texts = " ".join(
        str(getattr(b, "text", "")) for e in events if e.type == "user" for b in e.message.content
    )
    assert "scheduled-task" in user_texts
    assert "ping" in user_texts


# ── durability ───────────────────────────────────────────────────────────────


async def test_sqlite_store_survives_reload(tmp_path: Any) -> None:
    from linch import Schedule, SqliteScheduleStore

    db = tmp_path / "schedules.db"
    schedule = Schedule(payload="durable", cron="*/5 * * * *", next_run=4242.0)
    async with SqliteScheduleStore(db) as store:
        await store.add(schedule)

    # Reopen the same file → the schedule is still there, intact.
    async with SqliteScheduleStore(db) as store2:
        loaded = await store2.list()
        assert len(loaded) == 1
        assert loaded[0].id == schedule.id
        assert loaded[0].cron == "*/5 * * * *"
        assert loaded[0].next_run == 4242.0
        assert await store2.remove(schedule.id) is True
        assert await store2.list() == []


async def test_sqlite_claim_due_is_atomic_across_store_instances(tmp_path: Any) -> None:
    from linch import Schedule, SqliteScheduleStore

    db = tmp_path / "schedules.db"
    schedule = Schedule(payload="run once", interval_s=60, next_run=1000.0)
    async with SqliteScheduleStore(db) as writer:
        await writer.add(schedule)

    first = SqliteScheduleStore(db)
    second = SqliteScheduleStore(db)
    try:
        claimed_a, claimed_b = await asyncio.gather(
            first.claim_due(1000.0),
            second.claim_due(1000.0),
        )
    finally:
        await first.aclose()
        await second.aclose()

    claimed = claimed_a + claimed_b
    assert [s.id for s in claimed] == [schedule.id]

    async with SqliteScheduleStore(db) as reader:
        loaded = await reader.get(schedule.id)
        assert loaded is not None
        assert loaded.next_run == 1060.0


async def test_two_scheduler_loops_do_not_double_fire_sqlite_schedule(tmp_path: Any) -> None:
    from linch import Schedule, SchedulerLoop, SqliteScheduleStore

    db = tmp_path / "schedules.db"
    schedule = Schedule(payload="cluster tick", interval_s=60, next_run=1000.0)
    async with SqliteScheduleStore(db) as writer:
        await writer.add(schedule)

    store_a = SqliteScheduleStore(db)
    store_b = SqliteScheduleStore(db)
    session_a = _FakeSession()
    session_b = _FakeSession()
    loop_a = SchedulerLoop(store_a, session_a, clock=lambda: 1000.0)
    loop_b = SchedulerLoop(store_b, session_b, clock=lambda: 1000.0)
    try:
        fired_a, fired_b = await asyncio.gather(loop_a.tick(), loop_b.tick())
    finally:
        await store_a.aclose()
        await store_b.aclose()

    assert [s.id for s in fired_a + fired_b] == [schedule.id]
    notifications = session_a.pending_notifications + session_b.pending_notifications
    assert len(notifications) == 1
    assert "cluster tick" in notifications[0].content[0].text


async def test_claim_tick_isolates_a_failing_fire_from_sibling_schedules(tmp_path: Any) -> None:
    # Regression: with a claiming store, claim_due advances+commits every due
    # schedule's next_run before _fire runs. A raising on_event sink for one
    # claimed schedule must not abort delivery of the others claimed this tick.
    from linch import Schedule, SchedulerLoop, SqliteScheduleStore
    from linch.events import ScheduleEvent

    db = tmp_path / "schedules.db"
    async with SqliteScheduleStore(db) as writer:
        await writer.add(Schedule(id="a", payload="alpha", interval_s=60, next_run=1000.0))
        await writer.add(Schedule(id="b", payload="bravo", interval_s=60, next_run=1000.0))

    def explode_on_a(event: ScheduleEvent) -> None:
        if event.schedule_id == "a":
            raise RuntimeError("sink failed for a")

    store = SqliteScheduleStore(db)
    session = _FakeSession()
    loop = SchedulerLoop(store, session, clock=lambda: 1000.0, on_event=explode_on_a)
    try:
        fired = await loop.tick()
    finally:
        await store.aclose()

    # Both schedules were claimed and both deliveries landed despite a's sink
    # raising — the failure did not drop the sibling b.
    assert {s.id for s in fired} == {"a", "b"}
    payloads = "".join(msg.content[0].text for msg in session.pending_notifications)
    assert "alpha" in payloads and "bravo" in payloads


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_occurrence_materialization_advances_atomically_and_survives_remove(
    backend: str, tmp_path: Any
) -> None:
    from linch.coordination.scheduling import InMemoryScheduleStore, Schedule, SqliteScheduleStore

    store = (
        InMemoryScheduleStore()
        if backend == "memory"
        else SqliteScheduleStore(tmp_path / "occurrences.db")
    )
    try:
        schedule = Schedule(
            id="nightly",
            payload="original",
            interval_s=60,
            next_run=1000.0,
            metadata={"revision": 1},
        )
        await store.add(schedule)

        # limit=0 still atomically materializes and advances; it only declines
        # to lease an occurrence to this caller.
        assert (
            await store.claim_due_occurrences(1000.0, owner="materializer", lease_s=30, limit=0)
            == []
        )
        advanced = await store.get(schedule.id)
        assert advanced is not None and advanced.next_run == 1060.0
        assert await store.remove(schedule.id)

        occurrences = await store.claim_due_occurrences(1001.0, owner="delivery", lease_s=30)
        assert len(occurrences) == 1
        occurrence = occurrences[0]
        assert occurrence.schedule_id == "nightly"
        assert occurrence.payload == "original"
        assert occurrence.metadata == {"revision": 1}
        assert occurrence.scheduled_for == 1000.0
    finally:
        close = getattr(store, "aclose", None)
        if close is not None:
            await close()


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_occurrence_lease_reclaim_and_stale_ack_are_token_fenced(
    backend: str, tmp_path: Any
) -> None:
    from linch.coordination.scheduling import InMemoryScheduleStore, Schedule, SqliteScheduleStore

    store = (
        InMemoryScheduleStore()
        if backend == "memory"
        else SqliteScheduleStore(tmp_path / "occurrences.db")
    )
    try:
        await store.add(Schedule(id="tick", payload="p", interval_s=60, next_run=1000.0))
        original = await store.claim_due_occurrences(1000.0, owner="one", lease_s=10)
        assert await store.claim_due_occurrences(1009.0, owner="two", lease_s=10) == []

        reclaimed = await store.claim_due_occurrences(1010.0, owner="two", lease_s=10)
        assert reclaimed[0].id == original[0].id
        assert reclaimed[0].token != original[0].token

        await store.ack_occurrences(original)
        assert await store.claim_due_occurrences(1011.0, owner="three", lease_s=10) == []
        await store.release_occurrences(reclaimed)
        released = await store.claim_due_occurrences(1011.0, owner="three", lease_s=10)
        assert released[0].id == original[0].id
        await store.ack_occurrences(released)
        assert await store.claim_due_occurrences(1012.0, owner="four", lease_s=10) == []
    finally:
        close = getattr(store, "aclose", None)
        if close is not None:
            await close()


async def test_sqlite_occurrence_claims_are_shared_and_persist_across_restart(
    tmp_path: Any,
) -> None:
    from linch.coordination.scheduling import Schedule, SqliteScheduleStore

    path = tmp_path / "occurrences.db"
    async with SqliteScheduleStore(path) as writer:
        await writer.add(Schedule(id="a", payload="a", interval_s=60, next_run=1000.0))
        await writer.add(Schedule(id="b", payload="b", interval_s=60, next_run=1000.0))
        first = await writer.claim_due_occurrences(1000.0, owner="first", lease_s=10, limit=1)

    left = SqliteScheduleStore(path)
    right = SqliteScheduleStore(path)
    try:
        # The active lease on one occurrence does not prevent another consumer
        # from leasing a distinct materialized occurrence.
        second, blocked = await asyncio.gather(
            left.claim_due_occurrences(1001.0, owner="second", lease_s=10, limit=1),
            right.claim_due_occurrences(1001.0, owner="third", lease_s=10, limit=1),
        )
        assert len(second + blocked) == 1
        assert {first[0].id, (second + blocked)[0].id}.__len__() == 2

        reclaimed = await left.claim_due_occurrences(1010.0, owner="reclaimer", lease_s=10)
        assert {occurrence.id for occurrence in reclaimed} == {first[0].id}
    finally:
        await left.aclose()
        await right.aclose()


async def test_sqlite_schedule_adds_occurrence_outbox_to_legacy_database(tmp_path: Any) -> None:
    from linch.coordination.scheduling import SqliteScheduleStore

    path = tmp_path / "legacy-schedules.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE schedules (
            id TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            cron TEXT,
            interval_s REAL,
            next_run REAL,
            enabled INTEGER NOT NULL,
            created_at REAL,
            metadata TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO schedules(
            id, payload, cron, interval_s, next_run, enabled, created_at, metadata
        ) VALUES (?, ?, NULL, ?, ?, 1, NULL, ?)
        """,
        ("legacy", "old payload", 60.0, 1000.0, json.dumps({"old": True})),
    )
    conn.commit()
    conn.close()

    async with SqliteScheduleStore(path) as store:
        occurrences = await store.claim_due_occurrences(1000.0, owner="migration", lease_s=30)
        assert [(item.schedule_id, item.payload) for item in occurrences] == [
            ("legacy", "old payload")
        ]
        loaded = await store.get("legacy")
        assert loaded is not None and loaded.next_run == 1060.0


# ── tools ────────────────────────────────────────────────────────────────────


async def test_schedule_tools_create_list_cancel_and_reject_invalid() -> None:
    from linch import InMemoryScheduleStore, schedule_tools

    store = InMemoryScheduleStore()
    create, list_, cancel = schedule_tools(store, clock=lambda: 1000.0)

    res = await create.execute({"payload": "p", "cron": "* * * * *"}, ctx=None)
    assert not res.is_error
    sid = res.metadata["id"]
    assert (await store.get(sid)) is not None

    # Invalid cron is rejected at register time (no schedule stored).
    bad = await create.execute({"payload": "p", "cron": "nope"}, ctx=None)
    assert bad.is_error
    assert len(await store.list()) == 1

    listed = await list_.execute({}, ctx=None)
    assert sid in listed.content

    cancelled = await cancel.execute({"id": sid}, ctx=None)
    assert not cancelled.is_error
    assert await store.list() == []


async def test_agent_autoregisters_schedule_tools() -> None:
    from linch import Agent, InMemoryScheduleStore
    from linch.sessions import InMemorySessionStore

    agent = Agent(
        model="m",
        provider=cast(Any, object()),
        session_store=InMemorySessionStore(),
        cwd=".",
        schedule_store=InMemoryScheduleStore(),
    )
    names = {tool.name for tool in agent.tools.list()}
    assert {"CreateSchedule", "ListSchedules", "CancelSchedule"} <= names


def test_no_schedule_store_registers_nothing() -> None:
    from linch import Agent
    from linch.sessions import InMemorySessionStore

    agent = Agent(
        model="m",
        provider=cast(Any, object()),
        session_store=InMemorySessionStore(),
        cwd=".",
    )
    names = {tool.name for tool in agent.tools.list()}
    assert "CreateSchedule" not in names
    assert agent.schedule_store is None
