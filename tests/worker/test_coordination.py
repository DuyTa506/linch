"""Completion wakes release fleet capacity without idle unlimited-worker probes."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from test_dispatch import make_agent, running, wait_for


def count_dequeues(worker, monkeypatch, counts):
    original_manager = worker._make_manager

    def make_manager(connection):
        manager = original_manager(connection)
        original_dequeue = manager.queries.dequeue

        async def dequeue(*args, **kwargs):
            jobs = await original_dequeue(*args, **kwargs)
            counts[0] += 1
            counts[1] += not jobs
            return jobs

        monkeypatch.setattr(manager.queries, "dequeue", dequeue)
        return manager

    monkeypatch.setattr(worker, "_make_manager", make_manager)


async def quiet(counts):
    """Drain admission hints before measuring a window shorter than the long poll."""
    async with asyncio.timeout(3):
        while True:
            before = [value.copy() for value in counts]
            await asyncio.sleep(0.1)
            if counts == before:
                return


async def test_unlimited_completions_do_not_trigger_fleet_dequeues(pool, monkeypatch):
    from linch.evals import ScriptedProvider, TextTurn
    from linch.worker import Worker, enqueue_run

    measurements = []
    for fleet in (1, 2):
        gates = {f"completion-{fleet}-{i}": asyncio.Event() for i in range(3)}
        started = set()

        class Gated(ScriptedProvider):
            def __init__(self, rid, gate, entered):
                super().__init__([TextTurn("done")])
                self.rid = rid
                self.gate = gate
                self.entered = entered

            async def stream(self, request, **kwargs):
                self.entered.add(self.rid)
                await self.gate.wait()
                async for event in super().stream(request, **kwargs):
                    yield event

        async def factory(job, gates=gates, started=started, provider=Gated):
            return make_agent(job, provider(job.run.id, gates[job.run.id], started))

        counts = [[0, 0] for _ in range(fleet)]
        workers = [Worker(pool, factory, poll_interval_s=20) for _ in range(fleet)]
        for worker, counter in zip(workers, counts, strict=True):
            count_dequeues(worker, monkeypatch, counter)
        for rid in gates:
            await enqueue_run(pool, rid, "hello", run_id=rid)
        async with running(*workers):
            try:
                async with asyncio.timeout(5):
                    while len(started) != len(gates) or any(w._manager is None for w in workers):
                        await asyncio.sleep(0.01)
                await asyncio.gather(*(w._manager.listener_healthy() for w in workers))
                await quiet(counts)
                before = sum(value[1] for value in counts)
                for rid, gate in gates.items():
                    gate.set()
                    await wait_for(
                        lambda rid=rid: pool.fetchval(
                            "SELECT count(*)=0 FROM pgqueuer WHERE dedupe_key=$1", f"run:{rid}"
                        )
                    )
                    await quiet(counts)
                measurements.append(sum(value[1] for value in counts) - before)
            finally:
                for gate in gates.values():
                    gate.set()
        assert await pool.fetchval(
            "SELECT count(*) FROM runs WHERE id=ANY($1) AND status='completed'", list(gates)
        ) == len(gates)
        for rid in gates:
            rows = await workers[0]._run_store.load_events(rid)
            assert sum(row.event.type == "result" for row in rows) == 1
    assert measurements == [0, 0], f"completion-only empty dequeues M1/M2: {measurements}"


@pytest.mark.parametrize("queues", [{"default": 0}, {"default": 0, "limited": 1}])
async def test_driver_filters_only_valid_unlimited_completion_hints_and_removes_listener(queues):
    from linch.worker import Worker

    callbacks = set()

    class Connection:
        async def add_listener(self, channel, callback):
            callbacks.add((channel, callback))

        async def remove_listener(self, channel, callback):
            callbacks.remove((channel, callback))

    worker = Worker(SimpleNamespace(get_max_size=lambda: 2), lambda job: None, queues=queues)
    manager = worker._make_manager(Connection())
    driver = manager.queries.driver
    received = []
    await driver.add_listener("channel", received.append)
    await driver.add_listener("channel", received.append)
    assert len(callbacks) == 1
    ((_, relay),) = callbacks
    envelopes = []
    for operation in ("insert", "update", "delete", "truncate"):
        envelopes.append(
            json.dumps(
                {
                    "type": "table_changed_event",
                    "operation": operation,
                    "channel": "channel",
                    "table": "pgqueuer",
                    "sent_at": datetime.now(UTC).isoformat(),
                }
            )
        )
    # Invalid typed completions, unknown events and non-JSON retain upstream diagnostics.
    envelopes += [
        '{"type":"table_changed_event","operation":"delete"}',
        '{"type":"unknown"}',
        "not json",
        json.dumps({"type": "health_check_event", "id": "x"}),
        json.dumps({"type": "cancellation_event", "ids": [1]}),
    ]
    for payload in envelopes:
        relay(None, None, "channel", payload)
    expected = envelopes if any(queues.values()) else envelopes[:2] + envelopes[4:]
    assert received == expected
    await driver.remove_listener("channel", received.append)
    assert not callbacks


async def test_idle_timeout_uses_configured_poll_without_database_probes():
    from linch.worker import Worker

    worker = Worker(SimpleNamespace(get_max_size=lambda: 2), lambda job: None)
    manager = worker._make_manager(None)
    # Any upstream eligibility/ETA/count probe would try to use this absent connection.
    assert await manager._effective_dequeue_timeout(timedelta(seconds=7)) == timedelta(seconds=7)


async def test_limited_queue_completion_claims_successor_before_long_poll(pool, monkeypatch):
    from linch.evals import ScriptedProvider, TextTurn
    from linch.worker import Worker, enqueue_run

    release = asyncio.Event()
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    starts = []
    active = peak = 0
    loop = asyncio.get_running_loop()

    class Gated(ScriptedProvider):
        async def stream(self, request, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            starts.append(loop.time())
            try:
                if len(starts) == 1:
                    first_started.set()
                    await release.wait()
                else:
                    second_started.set()
                async for event in super().stream(request, **kwargs):
                    yield event
            finally:
                active -= 1

    async def factory(job):
        return make_agent(job, Gated([TextTurn("done")]))

    counts = [[0, 0], [0, 0]]
    workers = [Worker(pool, factory, queues={"default": 1}, poll_interval_s=20) for _ in counts]
    for worker, counter in zip(workers, counts, strict=True):
        count_dequeues(worker, monkeypatch, counter)
    for i in range(2):
        await enqueue_run(pool, f"limited-{i}", "hello")
    deletes = []

    def notice(connection, pid, channel, payload):
        if json.loads(payload).get("operation") == "delete":
            deletes.append(loop.time())

    async with pool.acquire() as connection:
        await connection.add_listener("ch_pgqueuer", notice)
        async with running(*workers):
            try:
                await asyncio.wait_for(first_started.wait(), 3)
                # Drain INSERT hints; baseline may still poll blocked work every 100ms.
                await asyncio.sleep(0.3)
                assert not second_started.is_set()
                release.set()
                await asyncio.wait_for(second_started.wait(), 1)
                await wait_for(lambda: pool.fetchval("SELECT count(*)=0 FROM pgqueuer"))
            finally:
                release.set()
        await connection.remove_listener("ch_pgqueuer", notice)
    assert peak == 1 and len(starts) == 2
    assert deletes
    print(f"limited completion DELETE-to-start ms: {(starts[1] - deletes[0]) * 1000:.3f}")


async def test_filtered_worker_preserves_real_health_and_cancellation(pool):
    from pgqueuer import Queries

    from linch.evals import ScriptedProvider, TextTurn
    from linch.worker import Worker, enqueue_run

    entered, release = asyncio.Event(), asyncio.Event()

    class Gated(ScriptedProvider):
        async def stream(self, request, **kwargs):
            entered.set()
            await release.wait()
            async for event in super().stream(request, **kwargs):
                yield event

    async def factory(job):
        return make_agent(job, Gated([TextTurn("done")]))

    rid = await enqueue_run(pool, "cancel", "hello")
    worker = Worker(pool, factory, poll_interval_s=0.05, retry_delay_s=0.05)
    async with running(worker):
        try:
            await asyncio.wait_for(entered.wait(), 3)
            await worker._manager.listener_healthy(timeout=timedelta(seconds=1))
            job_id = await pool.fetchval(
                "SELECT id FROM pgqueuer WHERE dedupe_key=$1", f"run:{rid}"
            )
            await Queries.from_asyncpg_pool(pool).notify_job_cancellation([job_id])
            async with asyncio.timeout(1):
                while not worker._manager.job_context[job_id].cancellation.cancel_called:
                    await asyncio.sleep(0.005)
        finally:
            release.set()
        await wait_for(lambda: pool.fetchval("SELECT count(*)=0 FROM pgqueuer"))
    assert await pool.fetchval("SELECT status FROM runs WHERE id=$1", rid) == "completed"


@pytest.mark.parametrize("transition", ["deferred", "locked"])
async def test_polling_finds_work_after_its_insert_hint_is_consumed(pool, monkeypatch, transition):
    from linch.worker import Worker, enqueue_run

    entered = asyncio.Event()

    async def factory(job):
        entered.set()
        return make_agent(job)

    rid = await enqueue_run(pool, "polling", "hello")
    if transition == "deferred":
        await pool.execute(
            "UPDATE pgqueuer SET execute_after=NOW()+INTERVAL '0.4 seconds' WHERE dedupe_key=$1",
            f"run:{rid}",
        )
    worker = Worker(pool, factory, poll_interval_s=0.15)
    counts = [0, 0]
    count_dequeues(worker, monkeypatch, counts)
    async with pool.acquire() as connection:
        transaction = connection.transaction()
        await transaction.start()
        committed = False
        try:
            if transition == "locked":
                await connection.fetchrow("SELECT id FROM pgqueuer FOR UPDATE")
            async with running(worker):
                try:
                    async with asyncio.timeout(3):
                        while counts[1] == 0:
                            await asyncio.sleep(0.005)
                    await worker._manager.listener_healthy(timeout=timedelta(seconds=1))
                    assert not entered.is_set()
                finally:
                    await transaction.commit()
                    committed = True
                # No new INSERT follows initial dequeue; eligibility/lock release needs polling.
                await asyncio.wait_for(entered.wait(), 2)
                await wait_for(lambda: pool.fetchval("SELECT count(*)=0 FROM pgqueuer"))
        finally:
            if not committed:
                await transaction.rollback()
    assert await pool.fetchval("SELECT status FROM runs WHERE id=$1", rid) == "completed"
