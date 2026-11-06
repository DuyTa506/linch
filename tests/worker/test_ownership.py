"""Ownership failures leave resumable work, never a terminal aborted run."""

from __future__ import annotations

import asyncio

import pytest
from test_dispatch import make_agent, running, wait_for


async def test_lease_lost_cancels_run_without_terminal_status_and_preserves_job(pool, monkeypatch):
    from linch import LeaseLostError, PostgresRunStore
    from linch.ext.evals import ScriptedProvider, TextTurn
    from linch.worker import Worker, enqueue_run

    entered = asyncio.Event()
    stopped = asyncio.Event()
    aborted = []

    class WaitingProvider(ScriptedProvider):
        async def stream(self, request):
            entered.set()
            try:
                await asyncio.Event().wait()
                async for event in super().stream(request):
                    yield event
            finally:
                aborted.append(bool(request.signal and request.signal.aborted))
                stopped.set()

    async def factory(job):
        return make_agent(job, WaitingProvider([TextTurn("unused")]))

    worker = Worker(pool, factory, lease_ttl_s=0.6, poll_interval_s=0.02, retry_delay_s=0.2)

    async def renew(store, lease):
        await entered.wait()
        worker.stop()  # Keep the first retry queued for inspection.
        raise LeaseLostError("ownership lost")

    monkeypatch.setattr(PostgresRunStore, "renew", renew)
    rid = await enqueue_run(pool, "lost", "hello")
    await asyncio.wait_for(worker.run(), 5)
    assert stopped.is_set()
    assert aborted == [False]
    assert await pool.fetchval("SELECT status FROM runs WHERE id=$1", rid) == "running"
    row = await pool.fetchrow("SELECT status::text,attempts FROM pgqueuer")
    assert row["status"] == "queued" and row["attempts"] == 1


async def test_expired_lease_without_takeover_retries_and_resumes_checkpoint(pool, monkeypatch):
    from linch import PostgresRunStore
    from linch.ext.evals import ScriptedProvider, TextTurn
    from linch.worker import Worker, enqueue_run

    first_epoch = None
    first_stopped = asyncio.Event()
    resumed = []
    real_renew = PostgresRunStore.renew

    async def renew(store, lease):
        nonlocal first_epoch
        if first_epoch is None:
            first_epoch = lease.epoch
        if lease.epoch == first_epoch:
            raise ConnectionError("renewal unavailable for a full TTL")
        return await real_renew(store, lease)

    class WaitingProvider(ScriptedProvider):
        async def stream(self, request):
            try:
                await asyncio.Event().wait()
                async for event in super().stream(request):
                    yield event
            finally:
                assert not request.signal or not request.signal.aborted
                first_stopped.set()

    async def factory(job):
        resumed.append(job.run.checkpoint.phase if job.run.checkpoint else None)
        if not resumed[:-1]:
            return make_agent(job, WaitingProvider([TextTurn("unused")]))
        return make_agent(job)

    monkeypatch.setattr(PostgresRunStore, "renew", renew)
    rid = await enqueue_run(pool, "ttl-expired", "hello")
    worker = Worker(pool, factory, lease_ttl_s=0.6, poll_interval_s=0.02, retry_delay_s=0.1)
    async with running(worker):
        await wait_for(
            lambda: pool.fetchval("SELECT status='completed' FROM runs WHERE id=$1", rid)
        )
        await wait_for(lambda: pool.fetchval("SELECT count(*)=0 FROM pgqueuer"))
    assert first_stopped.is_set()
    assert resumed == [None, "provider_pending"]
    assert await pool.fetchval("SELECT count(*) FROM runs") == 1
    assert await pool.fetchval("SELECT count(*) FROM pgqueuer_log WHERE status='queued'") >= 1


async def test_pgqueuer_stale_retry_requeues_repicked_row_but_d1_lease_still_excludes_a(pool):
    from datetime import timedelta
    from uuid import uuid4

    from pgqueuer import Queries
    from pgqueuer.ports.repository import EntrypointExecutionParameter
    from pgqueuer.types import QueueEntrypoint, QueueManagerId

    from linch import PostgresRunStore, RunLeaseConflictError
    from linch.worker import enqueue_run

    rid = await enqueue_run(pool, "repicked", "hello")
    queries = Queries.from_asyncpg_pool(pool)
    params = {QueueEntrypoint("linch.run:default"): EntrypointExecutionParameter(1)}
    (a,) = await queries.dequeue(1, params, QueueManagerId(uuid4()), None, timedelta(seconds=1))
    await pool.execute("UPDATE pgqueuer SET heartbeat=now()-interval '2 seconds'")
    owner_b = QueueManagerId(uuid4())
    (b,) = await queries.dequeue(1, params, owner_b, None, timedelta(seconds=1))
    assert b.id == a.id
    assert await pool.fetchval("SELECT queue_manager_id FROM pgqueuer") == owner_b
    store_b = PostgresRunStore("", pool=pool)
    lease_b = await store_b.claim(rid, "b", 30)
    store_a = PostgresRunStore("", pool=pool)
    with pytest.raises(RunLeaseConflictError):
        await store_a.claim(rid, "a", 30)
    # Source characterization: retry_job WHERE id=$1 has no queue-manager fence.
    await queries.retry_job(a, timedelta(seconds=0.1), None)
    row = await pool.fetchrow("SELECT status::text,attempts,queue_manager_id FROM pgqueuer")
    assert dict(row) == {"status": "queued", "attempts": 1, "queue_manager_id": None}
    assert (await store_b.renew(lease_b)).epoch == lease_b.epoch
    with pytest.raises(RunLeaseConflictError):
        await store_a.claim(rid, "a", 30)
    await store_b.release(lease_b)


async def test_successful_renewal_refreshes_budget_at_reply(pool, monkeypatch):
    from linch import PostgresRunStore
    from linch.worker import Worker, enqueue_run

    async def factory(job):
        return make_agent(job)

    real_renew = PostgresRunStore.renew

    async def renew(store, lease):
        await asyncio.sleep(0.25)
        return await real_renew(store, lease)

    monkeypatch.setattr(PostgresRunStore, "renew", renew)
    rid = await enqueue_run(pool, "slow-renew", "hello")
    store = PostgresRunStore("", pool=pool)
    lease = await store.claim(rid, "owner", 0.6)

    async def blocked():
        await asyncio.Event().wait()

    run_task = asyncio.create_task(blocked())
    worker = Worker(pool, factory, lease_ttl_s=0.6)
    heartbeat = asyncio.create_task(worker._heartbeat(store, lease, run_task))
    try:
        await asyncio.sleep(0.95)
        assert not run_task.done()
    finally:
        heartbeat.cancel()
        run_task.cancel()
        await asyncio.gather(heartbeat, run_task, return_exceptions=True)
        await store.release(lease)


async def _picked_job(pool):
    from datetime import timedelta
    from uuid import uuid4

    from pgqueuer import Queries
    from pgqueuer.ports.repository import EntrypointExecutionParameter
    from pgqueuer.types import QueueEntrypoint, QueueManagerId

    jobs = await Queries.from_asyncpg_pool(pool).dequeue(
        1,
        {QueueEntrypoint("linch.run:default"): EntrypointExecutionParameter(0)},
        QueueManagerId(uuid4()),
        None,
        timedelta(seconds=30),
    )
    return jobs[0]


async def test_cancellation_during_cleanup_preserves_nonterminal_dispatch(pool, monkeypatch):
    from datetime import timedelta

    from pgqueuer import Queries

    from linch import Agent
    from linch.worker import Worker, enqueue_run

    entered = asyncio.Event()
    finish = asyncio.Event()
    real_close = Agent.close

    async def close(agent):
        entered.set()
        await finish.wait()
        await real_close(agent)

    async def factory(job):
        return make_agent(job)

    worker = Worker(pool, factory)

    async def nonterminal(execution, store, record, payload, lease):
        from linch import Agent, empty_tools
        from linch.ext.evals import ScriptedProvider
        from linch.sessions import InMemorySessionStore

        assert lease.run_id == record.id
        execution.agent = Agent(
            model="m",
            provider=ScriptedProvider([]),
            session_store=InMemorySessionStore(),
            run_store=store,
            tools=empty_tools(),
        )

    monkeypatch.setattr(worker, "_invoke", nonterminal)
    monkeypatch.setattr(Agent, "close", close)
    rid = await enqueue_run(pool, "cleanup-cancel", "hello")
    job = await _picked_job(pool)
    task = asyncio.create_task(worker._handle(job))
    await asyncio.wait_for(entered.wait(), 3)
    task.cancel()
    finish.set()
    with pytest.raises(RuntimeError, match="non-terminal"):
        await task
    assert await pool.fetchval("SELECT status FROM runs WHERE id=$1", rid) == "pending"
    assert await pool.fetchval("SELECT lease_owner FROM runs WHERE id=$1", rid) is None
    await Queries.from_asyncpg_pool(pool).retry_job(job, timedelta(0), None)
    assert await pool.fetchval("SELECT attempts FROM pgqueuer") == 1


async def test_trace_extraction_failure_does_not_leak_claimed_lease(pool):
    import json

    from linch.worker import Worker, enqueue_run

    async def factory(job):
        return make_agent(job)

    rid = await enqueue_run(pool, "bad-carrier", "hello")
    job = await _picked_job(pool)
    payload = json.loads(job.payload)
    payload["traceparent"] = 42
    job = job.model_copy(update={"payload": json.dumps(payload).encode()})
    worker = Worker(pool, factory)
    await worker._handle(job)
    assert await pool.fetchval("SELECT lease_owner FROM runs WHERE id=$1", rid) is None
