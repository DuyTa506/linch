"""Completion wakes release fleet capacity without idle unlimited-worker probes."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from test_dispatch import make_agent, running, wait_for


def count_dequeues(monkeypatch):
    from pgqueuer import Queries

    counts, dequeue = [], Queries.dequeue

    async def counted(self, *args, **kwargs):
        jobs = await dequeue(self, *args, **kwargs)
        counts.append(not jobs)
        return jobs

    monkeypatch.setattr(Queries, "dequeue", counted)
    return counts


async def quiet(counts):
    async with asyncio.timeout(3):
        while True:
            before = len(counts)
            await asyncio.sleep(0.1)
            if len(counts) == before:
                return


def gated_factory(ids):
    from linch.evals import ScriptedProvider, TextTurn

    state = SimpleNamespace(
        gates={rid: asyncio.Event() for rid in ids}, starts=[], active=0, peak=0
    )

    async def factory(job):
        class Gated(ScriptedProvider):
            async def stream(self, request, **kwargs):
                state.starts.append(job.run.id)
                state.active += 1
                state.peak = max(state.peak, state.active)
                try:
                    await state.gates[job.run.id].wait()
                    async for event in super().stream(request, **kwargs):
                        yield event
                finally:
                    state.active -= 1

        return make_agent(job, Gated([TextTurn("done")]))

    return factory, state


@pytest.mark.parametrize("fleet", [1, 2])
async def test_unlimited_completions_do_not_trigger_fleet_dequeues(pool, monkeypatch, fleet):
    from linch.worker import Worker, enqueue_run

    counts = count_dequeues(monkeypatch)
    factory, state = gated_factory([f"completion-{fleet}-{i}" for i in range(3)])
    workers = [Worker(pool, factory, poll_interval_s=20) for _ in range(fleet)]
    for rid in state.gates:
        await enqueue_run(pool, rid, "hello", run_id=rid)
    async with running(*workers):
        try:
            async with asyncio.timeout(5):
                while len(state.starts) != 3 or any(w._manager is None for w in workers):
                    await asyncio.sleep(0.01)
            await asyncio.gather(*(w._manager.listener_healthy() for w in workers))
            await quiet(counts)
            before = sum(counts)
            for rid, gate in state.gates.items():
                gate.set()
                await wait_for(
                    lambda rid=rid: pool.fetchval(
                        "SELECT count(*)=0 FROM pgqueuer WHERE dedupe_key=$1", f"run:{rid}"
                    )
                )
                await quiet(counts)
            assert sum(counts) - before == 0, f"completion-only empty dequeues M{fleet}"
        finally:
            for gate in state.gates.values():
                gate.set()
    statuses = await pool.fetch("SELECT status FROM runs WHERE id=ANY($1)", list(state.gates))
    assert [row["status"] for row in statuses] == ["completed"] * 3
    for rid in state.gates:
        events = await workers[0]._run_store.load_events(rid)
        assert sum(row.event.type == "result" for row in events) == 1


@pytest.mark.parametrize("queues", [{"default": 0}, {"default": 0, "limited": 1}])
async def test_driver_filters_only_valid_unlimited_completion_hints_and_removes_listener(queues):
    from pgqueuer import QueueManager

    from linch.worker import Worker

    assert callable(getattr(QueueManager, "_effective_dequeue_timeout", None))
    assert callable(getattr(QueueManager, "fetch_jobs", None))
    callbacks = set()

    class Connection:
        async def add_listener(self, channel, callback):
            callbacks.add((channel, callback))

        async def remove_listener(self, channel, callback):
            callbacks.remove((channel, callback))

    worker = Worker(SimpleNamespace(get_max_size=lambda: 2), lambda job: None, queues=queues)
    manager = worker._make_manager(Connection())
    # A timeout probe would fail: this fake connection has no SQL methods.
    assert await manager._effective_dequeue_timeout(timedelta(seconds=7)) == timedelta(seconds=7)
    driver = manager.queries.driver
    received = []
    await driver.add_listener("channel", received.append)
    await driver.add_listener("channel", received.append)
    assert len(callbacks) == 1
    ((_, relay),) = callbacks
    event = dict(
        type="table_changed_event",
        channel="channel",
        table="pgqueuer",
        sent_at=datetime.now(UTC).isoformat(),
    )
    envelopes = [
        json.dumps(dict(event, operation=op)) for op in ("insert", "update", "delete", "truncate")
    ]
    envelopes += [
        '{"type":"table_changed_event","operation":"delete"}',
        '{"type":"unknown"}',
        "not json",
        '{"type":"health_check_event","id":"x"}',
        '{"type":"cancellation_event","ids":[1]}',
    ]
    for payload in envelopes:
        relay(None, None, "channel", payload)
    expected = envelopes if any(queues.values()) else envelopes[:2] + envelopes[4:]
    assert received == expected
    await driver.remove_listener("channel", received.append)
    assert not callbacks


async def test_limited_queue_completion_claims_successor_before_long_poll(pool):
    from linch.worker import Worker, enqueue_run

    factory, state = gated_factory(["limited-0", "limited-1"])
    workers = [Worker(pool, factory, queues={"default": 1}, poll_interval_s=20) for _ in range(2)]
    for rid in state.gates:
        await enqueue_run(pool, rid, "hello", run_id=rid)
    async with running(*workers):
        try:
            async with asyncio.timeout(3):
                while len(state.starts) != 1:
                    await asyncio.sleep(0.01)
            await asyncio.gather(*(w._manager.listener_healthy() for w in workers))
            await asyncio.sleep(0.3)  # Drain INSERT hints while the first job holds capacity.
            assert len(state.starts) == 1
            state.gates[state.starts[0]].set()
            # Completion must wake the successor before the 20s poll.
            async with asyncio.timeout(1):
                while len(state.starts) != 2:
                    await asyncio.sleep(0.01)
        finally:
            for gate in state.gates.values():
                gate.set()
        await wait_for(lambda: pool.fetchval("SELECT count(*)=0 FROM pgqueuer"))
    assert state.peak == 1 and len(state.starts) == 2
    for rid in state.gates:
        assert await pool.fetchval("SELECT status FROM runs WHERE id=$1", rid) == "completed"
        events = await workers[0]._run_store.load_events(rid)
        assert sum(row.event.type == "result" for row in events) == 1
