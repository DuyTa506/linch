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
from datetime import datetime, timezone
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
