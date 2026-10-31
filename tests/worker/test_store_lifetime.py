"""Worker-lifetime stores and verified borrowed pool contracts."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_dispatch import make_agent

from linch.storage._pg import _reset_connection, close_pool, ensure_pool
from linch.worker.dispatch import _BorrowedPool


class SchemaPool:
    def __init__(self):
        self.calls = 0
        self.closed = False
        self.failure = None
        self.fail_at = "transaction"

    @asynccontextmanager
    async def acquire(self):
        self.calls += 1
        yield self
        if self.fail_at == "acquire" and self.failure:
            raise self.failure()

    @asynccontextmanager
    async def transaction(self):
        yield
        if self.fail_at == "transaction" and self.failure:
            raise self.failure()

    async def execute(self, *args):
        await asyncio.sleep(0)

    async def fetchval(self, *args):
        return False

    async def close(self):
        self.closed = True


def adapter(pool):
    return SimpleNamespace(_pool=pool, _initialized=False, _init_lock=asyncio.Lock())


async def verify(store, *, schema_key="run"):
    return await ensure_pool(store, "schema", lambda: None, schema_key=schema_key, tables=())


async def test_verified_pool_coalesces_initialization_and_borrowed_close_is_noop():
    raw = SchemaPool()
    pool = _BorrowedPool(raw)
    stores = [adapter(pool) for _ in range(8)]
    await asyncio.gather(*(verify(store) for store in stores))
    assert raw.calls == 1
    await close_pool(stores[0], reset_initialized=True)
    assert stores[0]._initialized and stores[0]._pool is pool
    await verify(adapter(pool))
    assert raw.calls == 1 and not raw.closed
    await verify(adapter(pool), schema_key="session")
    assert raw.calls == 2
    await verify(adapter(_BorrowedPool(raw)))
    assert raw.calls == 3


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
@pytest.mark.parametrize("fail_at", ["transaction", "acquire"])
async def test_failed_verification_preserves_borrowed_pool_and_retries(failure, fail_at):
    raw = SchemaPool()
    raw.failure, raw.fail_at = failure, fail_at
    pool = _BorrowedPool(raw)
    store = adapter(pool)
    with pytest.raises(failure):
        await verify(store)
    assert store._pool is pool and not store._initialized and not raw.closed
    raw.failure = None
    await verify(store)
    await verify(adapter(pool))
    assert raw.calls == 2


async def test_raw_pool_close_and_schema_failure_keep_existing_lifecycle():
    raw = SchemaPool()
    store = adapter(raw)
    await verify(store)
    await close_pool(store, reset_initialized=True)
    assert raw.closed and store._pool is None and not store._initialized
    raw = SchemaPool()
    raw.failure = RuntimeError
    store = adapter(raw)
    with pytest.raises(RuntimeError):
        await verify(store)
    assert raw.closed and store._pool is None and not store._initialized


def benchmark_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "benchmark_runtime.py"
    spec = importlib.util.spec_from_file_location("worker_benchmark", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


async def measured_pool(pool):
    import asyncpg

    benchmark = benchmark_module()

    class Metrics(benchmark.ScaleStoreMetrics):
        def __init__(self):
            super().__init__()
            self.sql = Counter()

        def record_protocol_exchange(self, store, method, query=None):
            super().record_protocol_exchange(store, method, query)
            if query is not None and method in {
                "query",
                "bind_execute",
                "bind_execute_many",
                "execute",
            }:
                self.sql[str(query)] += 1

    metrics = Metrics()
    schema = await pool.fetchval("SELECT current_schema()")
    measured = await asyncpg.create_pool(
        os.environ["AGENT_KIT_TEST_PG_DSN"],
        min_size=8,
        max_size=8,
        server_settings={"search_path": schema},
        connection_class=benchmark._instrument_connection_class(metrics, "worker"),
        reset=_reset_connection,
    )

    async def warm_codecs():
        async with measured.acquire() as conn:
            await conn.fetchval("SELECT ARRAY[]::BIGINT[]")
            await asyncio.sleep(0.01)

    await asyncio.gather(*(warm_codecs() for _ in range(8)))
    return measured, metrics


async def test_protocol_capture_includes_warm_cached_query(pool):
    measured, metrics = await measured_pool(pool)
    try:
        query = "SELECT version FROM linch_schema_meta WHERE store = $1"
        await measured.fetchval(query, "run")
        metrics.sql.clear()
        metrics.reset_round_trip_metrics()
        await measured.fetchval(query, "run")
        assert metrics.sql[query] == 1
        assert sum(metrics.protocol_exchanges.values()) == 1
    finally:
        await measured.close()


async def test_worker_jobs_reuse_verified_stores_without_schema_protocol_calls(pool):
    from linch import PostgresRunStore
    from linch.sessions.postgres import PostgresSessionStore
    from linch.worker import Worker, enqueue_run

    measured, metrics = await measured_pool(pool)
    handles = []
    peak = active = 0

    async def factory(job):
        nonlocal active, peak
        handles.append((job.run_store, job.session_store, job.pool))
        assert job.run_store._initialized and job.session_store._initialized
        agent = make_agent(job)
        assert agent.session_store is job.session_store
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return agent

    worker = Worker(measured, factory)

    async def jobs_instead_of_dequeue(**kwargs):
        startup = dict(metrics.sql)
        startup_cycles = sum(metrics.protocol_exchanges.values())
        submitted = [await enqueue_run(pool, f"lifetime-{i}", "hello") for i in range(8)]
        metrics.sql.clear()
        metrics.reset_round_trip_metrics()

        async def job(index):
            rid = submitted[index]
            payload = json.dumps(
                {"run_id": rid, "session_id": f"lifetime-{index}", "prompt": "hello"}
            ).encode()
            queued = SimpleNamespace(payload=payload)
            await worker._handle(queued)
            await worker._check_finished_job(queued)
            return rid

        ids = [await job(0), await job(1)]
        ids.extend(await asyncio.gather(*(job(i) for i in range(2, 8))))
        job_sql = dict(metrics.sql)
        cycles = sum(metrics.protocol_exchanges.values())
        schema = {
            q: n
            for q, n in job_sql.items()
            if any(
                s in q.lower()
                for s in (
                    "pg_attribute",
                    "to_regclass",
                    "linch_schema_meta",
                    "create table",
                    "create index",
                )
            )
        }
        advisory = sum(n for q, n in job_sql.items() if "pg_advisory" in q)
        print(
            {
                "startup_cycles": startup_cycles,
                "startup_schema": sum(
                    n
                    for q, n in startup.items()
                    if "linch_schema_meta" in q
                    or "to_regclass" in q
                    or "pg_attribute" in q
                    or "CREATE TABLE" in q
                ),
                "startup_advisory": sum(n for q, n in startup.items() if "pg_advisory" in q),
                "job_cycles": cycles,
                "job_schema": schema,
                "job_advisory": advisory,
                "jobs": len(ids),
            }
        )
        assert schema == {} and advisory == 0 and cycles > 0
        assert await measured.fetchval(
            "SELECT count(*) FROM runs WHERE status='completed' AND id=ANY($1::text[])", ids
        ) == len(ids)
        assert len(handles) == len(ids) and peak > 1
        assert all(all(a is b for a, b in zip(handles[0], item, strict=True)) for item in handles)
        run, session, borrowed = handles[0]
        assert run._initialized and session._initialized
        assert run._held_leases == run._lease_owners == run._lease_ttls == {}
        assert not worker._active_runs
        metrics.sql.clear()
        metrics.reset_round_trip_metrics()
        for cls in (PostgresRunStore, PostgresSessionStore):
            fresh = cls("", pool=borrowed)
            await fresh._ensure()
            await fresh.close()
            await fresh._ensure()
        assert sum(metrics.protocol_exchanges.values()) == 0
        assert not measured.is_closing()

    worker._manager.run = jobs_instead_of_dequeue
    try:
        await worker.run()
    finally:
        await measured.close()


async def test_stale_release_does_not_erase_new_claim_on_same_store(pool):
    from linch import LeaseLostError, PostgresRunStore

    store = PostgresRunStore("", pool=_BorrowedPool(pool))
    record = await store.create_run("stale-release")
    old = await store.claim(record.id, "old", 30)
    await pool.execute("UPDATE runs SET lease_expires_at=0 WHERE id=$1", record.id)
    new = await store.claim(record.id, "new", 30)
    with pytest.raises(LeaseLostError):
        await store.release(old)
    assert store._held_leases[record.id] == new.epoch
    assert store._lease_owners[record.id] == new.owner
    await store.renew(new)
    await store.release(new)
    assert store._held_leases == store._lease_owners == store._lease_ttls == {}


@pytest.mark.parametrize("wrong", ["run_store", "session_store"])
async def test_invalid_factory_teardown_preserves_the_other_worker_store(pool, wrong):
    from linch import InMemoryRunStore
    from linch.sessions import InMemorySessionStore
    from linch.worker import Worker, enqueue_run

    closed = []

    async def factory(job):
        from linch.evals import ScriptedProvider, TextTurn

        class Provider(ScriptedProvider):
            async def close(self):
                closed.append(job.run.id)

        agent = make_agent(job, Provider([TextTurn("unused")]))
        if wrong == "run_store":
            agent.run_store = InMemoryRunStore()
        else:
            agent.session_store = InMemorySessionStore()
        return agent

    worker = Worker(pool, factory)
    rid = await enqueue_run(pool, f"invalid-{wrong}", "hello")
    await worker._run_store._ensure()
    await worker._session_store._ensure()
    await worker._handle(
        SimpleNamespace(
            payload=json.dumps(
                {"run_id": rid, "session_id": f"invalid-{wrong}", "prompt": "hello"}
            ).encode()
        )
    )
    record = await worker._run_store.load_run(rid)
    assert record.status == "failed" and wrong in record.meta["errors"][-1]["message"]
    assert worker._run_store._initialized and worker._session_store._initialized
    assert worker._run_store._pool is worker._session_store._pool is worker._pool
    assert closed == [rid] and not pool.is_closing()


async def test_worker_startup_verification_failure_never_starts_jobs_and_retries(pool):
    from linch.worker import Worker

    worker = Worker(pool, lambda job: None)
    # The first startup installs the session schema; remove its required column
    # before retrying through a fresh wrapper to prove failed verification stays uncached.
    await worker._session_store._ensure()
    await pool.execute("ALTER TABLE sessions DROP COLUMN last_message_seq")
    retry = Worker(pool, lambda job: None)
    started = []

    async def manager(**kwargs):
        started.append(True)

    retry._manager.run = manager
    with pytest.raises(Exception, match="start a new run"):
        await retry.run()
    assert started == [] and not pool.is_closing()
    assert retry._session_store._pool is retry._pool
    await pool.execute("ALTER TABLE sessions ADD COLUMN last_message_seq BIGINT NOT NULL DEFAULT 0")
    await retry.run()
    assert started == [True]


async def test_suspended_stale_release_preserves_claim_published_during_await(pool, monkeypatch):
    from linch import LeaseLostError, PostgresRunStore

    store = PostgresRunStore("", pool=_BorrowedPool(pool))
    record = await store.create_run("release-race")
    old = await store.claim(record.id, "old", 30)
    entered, resume = asyncio.Event(), asyncio.Event()
    ensure = store._ensure

    async def gated_ensure():
        if asyncio.current_task().get_name() == "old-release":
            entered.set()
            await resume.wait()
        return await ensure()

    monkeypatch.setattr(store, "_ensure", gated_ensure)
    releasing = asyncio.create_task(store.release(old), name="old-release")
    try:
        await asyncio.wait_for(entered.wait(), 3)
        await pool.execute("UPDATE runs SET lease_expires_at=0 WHERE id=$1", record.id)
        new = await store.claim(record.id, "new", 30)
        resume.set()
        with pytest.raises(LeaseLostError):
            await releasing
        assert store._held_leases[record.id] == new.epoch
        assert store._lease_owners[record.id] == new.owner
        await store.renew(new)
        await store.release(new)
        assert store._held_leases == store._lease_owners == store._lease_ttls == {}
    finally:
        resume.set()
        await asyncio.gather(releasing, return_exceptions=True)
