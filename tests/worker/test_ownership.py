"""Ownership failures leave resumable work, never a terminal aborted run."""

from __future__ import annotations

import asyncio

import pytest
from test_dispatch import make_agent, running, wait_for


async def test_two_transient_renew_failures_then_success_complete_the_run(pool, monkeypatch):
    from linch import PostgresRunStore
    from linch.evals import ScriptedProvider, TextTurn
    from linch.worker import Worker, enqueue_run

    renewed = asyncio.Event()
    finish = asyncio.Event()
    calls = 0
    real_renew = PostgresRunStore.renew

    async def renew(store, lease):
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise ConnectionError("brief database blip")
        result = await real_renew(store, lease)
        renewed.set()
        finish.set()
        return result

    class WaitingProvider(ScriptedProvider):
        async def stream(self, request):
            await finish.wait()
            async for event in super().stream(request):
                yield event

    async def factory(job):
        return make_agent(job, WaitingProvider([TextTurn("done")]))

    monkeypatch.setattr(PostgresRunStore, "renew", renew)
    rid = await enqueue_run(pool, "transient", "hello")
    async with running(Worker(pool, factory, lease_ttl_s=0.6, poll_interval_s=0.02)):
        try:
            await asyncio.wait_for(renewed.wait(), 3)
            await wait_for(
                lambda: pool.fetchval("SELECT status='completed' FROM runs WHERE id=$1", rid)
            )
        finally:
            finish.set()
    assert calls >= 3
    assert await pool.fetchval("SELECT status FROM runs WHERE id=$1", rid) == "completed"


async def test_lease_lost_cancels_run_without_terminal_status_and_preserves_job(pool, monkeypatch):
    from linch import LeaseLostError, PostgresRunStore
    from linch.evals import ScriptedProvider, TextTurn
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
    from linch.evals import ScriptedProvider, TextTurn
    from linch.worker import Worker, enqueue_run

    first_store = None
    first_stopped = asyncio.Event()
    resumed = []
    real_renew = PostgresRunStore.renew

    async def renew(store, lease):
        if store is first_store:
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
        nonlocal first_store
        resumed.append(job.run.checkpoint.phase if job.run.checkpoint else None)
        if first_store is None:
            first_store = job.run_store
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


async def test_normal_nonterminal_exit_is_retried(pool, monkeypatch):
    from linch import Session
    from linch.worker import Worker, enqueue_run

    calls = 0
    real_run = Session.run

    async def empty():
        for event in ():
            yield event

    def run(session, prompt, opts=None):
        nonlocal calls
        calls += 1
        return empty() if calls == 1 else real_run(session, prompt, opts)

    async def factory(job):
        return make_agent(job)

    monkeypatch.setattr(Session, "run", run)
    rid = await enqueue_run(pool, "nonterminal", "hello")
    async with running(Worker(pool, factory, poll_interval_s=0.02, retry_delay_s=0.05)):
        await wait_for(
            lambda: pool.fetchval("SELECT status='completed' FROM runs WHERE id=$1", rid)
        )
    assert calls == 2
