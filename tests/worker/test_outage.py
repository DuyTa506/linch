"""Worker supervision preserves drain and restores a real LISTEN after database loss."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager, suppress
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import asyncpg
import pytest
from test_dispatch import make_agent, wait_for


class CyclePool:
    def __init__(self, max_size=12):
        self.max_size = max_size
        self.connections = []
        self.entered = asyncio.Event()
        self.acquire_gate = None
        self.closed = False
        self.released = []

    def get_max_size(self):
        return self.max_size

    @asynccontextmanager
    async def acquire(self, **kwargs):
        self.entered.set()
        if self.acquire_gate is not None:
            async with asyncio.timeout(kwargs.get("timeout")):
                await self.acquire_gate.wait()
        connection = SimpleNamespace(closed=False)

        async def close():
            connection.closed = True

        connection.close = close
        self.connections.append(connection)
        try:
            yield connection
        finally:
            await self.release(connection)

    async def release(self, connection):
        self.released.append(connection)

    async def close(self):
        self.closed = True


async def prepared(monkeypatch, pool=None):
    import linch.worker.runtime as runtime

    async def ready(*args):
        pass

    monkeypatch.setattr(runtime, "install", ready)
    worker = runtime.Worker(pool or CyclePool(), lambda job: None)
    monkeypatch.setattr(worker._run_store, "_ensure", ready)
    monkeypatch.setattr(worker._session_store, "_ensure", ready)
    return worker


@pytest.mark.parametrize("listener_failure", [False, True])
async def test_connection_loss_rebuilds_manager_after_unwind_with_same_stores(
    monkeypatch, listener_failure
):
    worker = await prepared(monkeypatch)
    stores = worker._run_store, worker._session_store, worker._pool, worker._active_runs
    cycles = []
    drained = False

    def manager(connection):
        shutdown = asyncio.Event()

        async def run(**kwargs):
            nonlocal drained
            assert kwargs["log_aggregation_interval"] == timedelta(0)
            assert not connection.closed
            if len(cycles) == 1:
                try:
                    from pgqueuer.errors import FailingListenerError

                    if listener_failure:
                        raise FailingListenerError
                    raise asyncpg.ConnectionDoesNotExistError("database lost")
                finally:
                    await asyncio.sleep(0)
                    drained = True
            assert drained and cycles[0][0].closed
            worker.stop()

        result = SimpleNamespace(run=run, shutdown=shutdown)
        cycles.append((connection, result))
        return result

    monkeypatch.setattr(worker, "_make_manager", manager, raising=False)
    await asyncio.wait_for(worker.run(), 2)
    assert len(cycles) == 2 and cycles[0][0] is not cycles[1][0]
    assert stores == (worker._run_store, worker._session_store, worker._pool, worker._active_runs)
    assert not worker._pool.closed


@pytest.mark.parametrize("when", ["before", "acquire", "backoff"])
async def test_stop_is_not_lost_between_manager_cycles(monkeypatch, when):
    import linch.worker.runtime as runtime

    pool = CyclePool()
    worker = await prepared(monkeypatch, pool)
    started = []
    backoff = asyncio.Event()

    def manager(connection):
        async def run(**kwargs):
            started.append(True)
            raise OSError("down")

        return SimpleNamespace(run=run, shutdown=asyncio.Event())

    monkeypatch.setattr(worker, "_make_manager", manager, raising=False)
    original_uniform = runtime.random.Random.uniform if hasattr(runtime, "random") else None

    def delay(self, low, high):
        backoff.set()
        return high

    if original_uniform:
        monkeypatch.setattr(runtime.random.Random, "uniform", delay)
    if when == "before":
        worker.stop()
    if when == "acquire":
        pool.acquire_gate = asyncio.Event()
    task = asyncio.create_task(worker.run())
    try:
        if when == "acquire":
            await asyncio.wait_for(pool.entered.wait(), 1)
        elif when == "backoff":
            await asyncio.wait_for(backoff.wait(), 1)
        worker.stop()
        await asyncio.wait_for(task, 2.5)
        assert len(started) == (1 if when == "backoff" else 0)
        assert not pool.closed
    finally:
        if pool.acquire_gate:
            pool.acquire_gate.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_repeated_cancellation_waits_for_manager_and_callbacks_to_drain(monkeypatch):
    worker = await prepared(monkeypatch)
    entered, draining, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    connection = None

    def manager(conn):
        nonlocal connection
        connection = conn
        shutdown = asyncio.Event()

        async def run(**kwargs):
            entered.set()
            await shutdown.wait()
            draining.set()
            await release.wait()
            assert not conn.closed

        return SimpleNamespace(run=run, shutdown=shutdown)

    monkeypatch.setattr(worker, "_make_manager", manager, raising=False)
    task = asyncio.create_task(worker.run())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        await asyncio.wait_for(draining.wait(), 1)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done() and not connection.closed
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert connection.closed and not worker._pool.closed
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    "failure", [ValueError("bad config"), asyncpg.UndefinedTableError("schema")]
)
async def test_configuration_errors_are_not_retried(monkeypatch, failure):
    worker = await prepared(monkeypatch)

    def manager(connection):
        async def run(**kwargs):
            raise failure

        return SimpleNamespace(run=run, shutdown=asyncio.Event())

    monkeypatch.setattr(worker, "_make_manager", manager, raising=False)
    with pytest.raises(type(failure), match=str(failure)):
        await asyncio.wait_for(worker.run(), 1)
    assert len(worker._pool.connections) == 1


@pytest.fixture
async def outage_database():
    """Disable connections only to an owned database, through a separate admin DB."""
    dsn = os.getenv("AGENT_KIT_TEST_PG_DSN")
    if not dsn:
        pytest.skip("AGENT_KIT_TEST_PG_DSN unavailable")
    admin = await asyncpg.connect(dsn)
    name = f"linch_outage_{uuid4().hex}"
    created = False
    try:
        try:
            await admin.execute(f'CREATE DATABASE "{name}"')
            created = True
        except asyncpg.InsufficientPrivilegeError:
            pytest.skip("outage regression requires CREATEDB")
        pool = await asyncpg.create_pool(
            dsn, database=name, min_size=1, max_size=12, server_settings={"search_path": "public"}
        )
        try:
            from linch.worker import install

            await pool.execute("CREATE SCHEMA IF NOT EXISTS public")
            await install(pool)
            yield admin, name, pool
        finally:
            await admin.execute(f'ALTER DATABASE "{name}" ALLOW_CONNECTIONS true')
            await pool.close()
    finally:
        if created:
            await admin.execute(f'ALTER DATABASE "{name}" ALLOW_CONNECTIONS true')
            await admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=$1", name
            )
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        await admin.close()


@pytest.mark.parametrize("stop_during_outage", [False, True])
async def test_three_second_database_outage_restores_dispatch_and_listen(
    outage_database, stop_during_outage
):
    from linch import ErrorEvent, ResultEvent
    from linch.evals import ScriptedProvider, TextTurn
    from linch.worker import Worker, enqueue_run

    admin, name, pool = outage_database
    entered, release = asyncio.Event(), asyncio.Event()
    handles = []

    class Interrupted(ScriptedProvider):
        async def stream(self, request, **kwargs):
            entered.set()
            await release.wait()
            async for event in super().stream(request, **kwargs):
                yield event

    async def factory(job):
        handles.append((job.run_store, job.session_store, job.pool))
        return make_agent(job, Interrupted([TextTurn("done")]))

    worker = Worker(pool, factory, lease_ttl_s=0.6, stall_timeout_s=1.2, poll_interval_s=0.05)
    rid = await enqueue_run(pool, "before", "hello")
    task = asyncio.create_task(worker.run())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        before = worker._manager
        assert await before.listener_healthy(timeout=timedelta(seconds=1))
        before_connection = getattr(before.queries.driver, "_connection", None)
        before_connection = before_connection or before.queries.driver._listener_connection
        before_pid = before_connection.get_server_pid()
        row = await pool.fetchrow("SELECT checkpoint,lease_epoch FROM runs WHERE id=$1", rid)
        assert row["checkpoint"] is not None
        queued = SimpleNamespace(id=await pool.fetchval("SELECT id FROM pgqueuer"))
        await admin.execute(f'ALTER DATABASE "{name}" ALLOW_CONNECTIONS false')
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=$1", name
        )
        release.set()

        # Wait for the manager's own query/unwind to observe the disconnect;
        # injecting a second query during asyncpg's terminate transition races
        # its protocol state instead of testing delayed queue buffer recovery.
        async def old_cycle_finished():
            return worker._manager is not before

        await wait_for(old_cycle_finished, 3)
        with pytest.raises(asyncpg.InterfaceError):
            await before.queries.retry_job(queued, timedelta(0), None)
        if stop_during_outage:
            worker.stop()
            await asyncio.wait_for(task, 3)
        await asyncio.sleep(3)
        assert stop_during_outage or not task.done(), task.exception() if task.done() else None
        await admin.execute(f'ALTER DATABASE "{name}" ALLOW_CONNECTIONS true')
        # A delayed held-status flush must never revive the old cycle after
        # recovery and strand a nonterminal run in queue status failed.
        with pytest.raises(asyncpg.InterfaceError):
            await before.queries.log_jobs([(queued, "failed", None)])
        if stop_during_outage:
            assert not worker._active_runs and not pool.is_closing()
            store = worker._run_store
            assert store._held_leases == store._lease_owners == store._lease_ttls == {}
            assert await pool.fetchval("SELECT 1") == 1
            return
        await wait_for(
            lambda: pool.fetchval(
                "SELECT status IN ('completed','failed','aborted') FROM runs WHERE id=$1", rid
            ),
            15,
        )
        after = worker._manager
        assert after is not before
        assert after.queries.driver._connection.get_server_pid() != before_pid
        assert await after.listener_healthy(timeout=timedelta(seconds=1))
        later = await enqueue_run(pool, "after", "hello")
        await wait_for(
            lambda: pool.fetchval("SELECT status='completed' FROM runs WHERE id=$1", later)
        )
        await wait_for(lambda: pool.fetchval("SELECT count(*)=0 FROM pgqueuer"))
        for run_id in (rid, later):
            record = await worker._run_store.load_run(run_id)
            rows = await worker._run_store.load_events(run_id)
            assert [row.seq for row in rows] == list(range(1, len(rows) + 1))
            assert sum(isinstance(row.event, ResultEvent) for row in rows) == 1
            assert isinstance(rows[-1].event, ResultEvent)
            assert any(isinstance(row.event, ErrorEvent) for row in rows) == (
                record.status == "failed"
            )
        if len(handles) > 2:
            assert (
                await pool.fetchval("SELECT lease_epoch FROM runs WHERE id=$1", rid)
                > row["lease_epoch"]
            )
        assert all(handle == handles[0] for handle in handles)
    finally:
        release.set()
        await admin.execute(f'ALTER DATABASE "{name}" ALLOW_CONNECTIONS true')
        worker.stop()
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
    assert not worker._active_runs
    store = worker._run_store
    assert store._held_leases == store._lease_owners == store._lease_ttls == {}
    assert not pool.is_closing() and await pool.fetchval("SELECT 1") == 1


async def test_reconnect_jitter_increases_then_stays_bounded(monkeypatch):
    import linch.worker.runtime as runtime

    worker = await prepared(monkeypatch)
    bounds = []
    attempts = 0

    def uniform(self, low, high):
        bounds.append((low, high))
        return 0

    def manager(connection):
        async def run(**kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 8:
                worker.stop()
            raise ConnectionError("down")

        return SimpleNamespace(run=run, shutdown=asyncio.Event())

    monkeypatch.setattr(runtime.random.Random, "uniform", uniform)
    monkeypatch.setattr(worker, "_make_manager", manager)
    await asyncio.wait_for(worker.run(), 1)
    assert bounds[:5] == [(0.05, 0.1), (0.1, 0.2), (0.2, 0.4), (0.4, 0.8), (0.8, 1.6)]
    assert bounds[5:] == [(1, 2), (1, 2), (1, 2)]


async def test_transient_startup_retries_without_replacing_worker_stores(monkeypatch):
    worker = await prepared(monkeypatch)
    store = worker._session_store
    attempts = 0

    async def ensure():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("startup database loss")

    def manager(connection):
        async def run(**kwargs):
            assert attempts == 2 and worker._session_store is store
            worker.stop()

        return SimpleNamespace(run=run, shutdown=asyncio.Event())

    monkeypatch.setattr(store, "_ensure", ensure)
    monkeypatch.setattr(worker, "_make_manager", manager)
    await asyncio.wait_for(worker.run(), 1)
    assert attempts == 2


async def test_normal_manager_return_keeps_serving_until_stop(monkeypatch):
    worker = await prepared(monkeypatch)
    cycles = []

    def manager(connection):
        async def run(**kwargs):
            cycles.append(connection)
            if len(cycles) == 2:
                worker.stop()

        return SimpleNamespace(run=run, shutdown=asyncio.Event())

    monkeypatch.setattr(worker, "_make_manager", manager)
    await asyncio.wait_for(worker.run(), 1)
    assert len(cycles) == 2 and all(conn.closed for conn in cycles)


async def test_statement_level_55000_is_not_retried(monkeypatch):
    worker = await prepared(monkeypatch)

    def manager(connection):
        async def run(**kwargs):
            raise asyncpg.ObjectNotInPrerequisiteStateError("invalid statement state")

        return SimpleNamespace(run=run, shutdown=asyncio.Event())

    monkeypatch.setattr(worker, "_make_manager", manager)
    with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
        await asyncio.wait_for(worker.run(), 1)


async def test_outage_fixture_skips_cleanly_without_createdb(monkeypatch):
    statements = []
    closed = []

    async def execute(sql):
        statements.append(sql)
        raise asyncpg.InsufficientPrivilegeError("CREATEDB denied")

    async def close():
        closed.append(True)

    async def connect(dsn):
        return SimpleNamespace(execute=execute, close=close)

    monkeypatch.setenv("AGENT_KIT_TEST_PG_DSN", "unused")
    monkeypatch.setattr(asyncpg, "connect", connect)
    fixture = outage_database.__wrapped__()
    with pytest.raises(pytest.skip.Exception, match="CREATEDB"):
        await anext(fixture)
    assert len(statements) == 1 and statements[0].startswith("CREATE DATABASE ")
    assert closed == [True]


async def test_host_cancellation_survives_drain_failure(monkeypatch):
    worker = await prepared(monkeypatch)
    entered, cleanup = asyncio.Event(), asyncio.Event()

    def manager(connection):
        shutdown = asyncio.Event()

        async def run(**kwargs):
            entered.set()
            await shutdown.wait()
            cleanup.set()
            raise RuntimeError("drain failure")

        return SimpleNamespace(run=run, shutdown=shutdown)

    monkeypatch.setattr(worker, "_make_manager", manager)
    task = asyncio.create_task(worker.run())
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleanup.is_set() and worker._pool.connections[0].closed


async def test_connection_close_loss_releases_and_reconnects(monkeypatch):
    pool = CyclePool()
    worker = await prepared(monkeypatch, pool)
    cycles = []

    def manager(connection):
        async def run(**kwargs):
            cycles.append(connection)
            if len(cycles) == 1:

                async def close():
                    connection.closed = True
                    raise OSError("connection disappeared during close")

                connection.close = close
                raise OSError("database lost")
            assert cycles[0].closed
            worker.stop()

        return SimpleNamespace(run=run, shutdown=asyncio.Event())

    monkeypatch.setattr(worker, "_make_manager", manager)
    await asyncio.wait_for(worker.run(), 1)
    assert len(cycles) == 2 and pool.released == cycles


def test_worker_rejects_single_connection_pool_before_acquisition():
    from linch.worker import Worker

    pool = CyclePool(max_size=1)
    with pytest.raises(ValueError, match="at least two connections"):
        Worker(pool, lambda job: None)
    assert not pool.entered.is_set() and not pool.closed


def test_worker_accepts_two_connection_pool():
    from linch.worker import Worker

    pool = CyclePool(max_size=2)
    worker = Worker(pool, lambda job: None)
    assert worker._pool.get_max_size() == 2
    assert not pool.entered.is_set() and not pool.closed
