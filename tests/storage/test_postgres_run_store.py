"""Local checks for the optional PostgreSQL run-store backend."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import time
from typing import Any
from uuid import uuid4

import pytest


def test_postgres_run_store_reports_optional_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    import linch.run_stores.postgres as postgres_module

    monkeypatch.setitem(sys.modules, "asyncpg", None)
    with pytest.raises(ModuleNotFoundError, match=r"linch\[postgres\]"):
        postgres_module.PostgresRunStore("postgresql://unused")


def _postgres_available() -> bool:
    return (
        bool(os.environ.get("AGENT_KIT_TEST_PG_DSN"))
        and importlib.util.find_spec("asyncpg") is not None
    )


needs_postgres = pytest.mark.skipif(
    not _postgres_available(), reason="AGENT_KIT_TEST_PG_DSN or asyncpg unavailable"
)


class _RollbackTransaction:
    """Proxy a real asyncpg transaction and force rollback at context exit."""

    def __init__(self, real: Any, failure_type: type[BaseException]) -> None:
        self._real = real
        self._failure_type = failure_type

    async def __aenter__(self) -> Any:
        return await self._real.__aenter__()

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        failure = (
            asyncio.CancelledError()
            if self._failure_type is asyncio.CancelledError
            else RuntimeError("forced claim transaction failure")
        )
        await self._real.__aexit__(self._failure_type, failure, tb)
        raise failure


class _RollbackConnection:
    def __init__(self, real: Any, failure_type: type[BaseException]) -> None:
        self._real = real
        self._failure_type = failure_type

    def transaction(self, *args: Any, **kwargs: Any) -> _RollbackTransaction:
        return _RollbackTransaction(self._real.transaction(*args, **kwargs), self._failure_type)

    async def fetchrow(self, query: str, *args: Any) -> Any:
        if "UPDATE runs" in query and "lease_owner" in query:
            failure = (
                asyncio.CancelledError()
                if self._failure_type is asyncio.CancelledError
                else RuntimeError("forced lease statement failure")
            )
            raise failure
        return await self._real.fetchrow(query, *args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class _RollbackAcquire:
    def __init__(self, real: Any, failure_type: type[BaseException]) -> None:
        self._real = real
        self._failure_type = failure_type

    async def __aenter__(self) -> _RollbackConnection:
        return _RollbackConnection(await self._real.__aenter__(), self._failure_type)

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return await self._real.__aexit__(exc_type, exc, tb)


class _RollbackPool:
    def __init__(self, real: Any, failure_type: type[BaseException]) -> None:
        self._real = real
        self._failure_type = failure_type

    def acquire(self, *args: Any, **kwargs: Any) -> _RollbackAcquire:
        return _RollbackAcquire(self._real.acquire(*args, **kwargs), self._failure_type)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class _TraceConnection:
    def __init__(self, real: Any, queries: list[str]) -> None:
        self._real = real
        self._queries = queries

    async def fetchrow(self, query: str, *args: Any) -> Any:
        self._queries.append(query)
        return await self._real.fetchrow(query, *args)

    async def fetchval(self, query: str, *args: Any) -> Any:
        self._queries.append(query)
        return await self._real.fetchval(query, *args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class _TraceAcquire:
    def __init__(self, real: Any, queries: list[str]) -> None:
        self._real = real
        self._queries = queries

    async def __aenter__(self) -> _TraceConnection:
        return _TraceConnection(await self._real.__aenter__(), self._queries)

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return await self._real.__aexit__(exc_type, exc, tb)


class _TracePool:
    def __init__(self, real: Any, queries: list[str]) -> None:
        self._real = real
        self._queries = queries

    def acquire(self, *args: Any, **kwargs: Any) -> _TraceAcquire:
        return _TraceAcquire(self._real.acquire(*args, **kwargs), self._queries)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class _DeleteRaceConnection(_TraceConnection):
    def __init__(self, real: Any, queries: list[str], run_id: str, delete_dsn: str) -> None:
        super().__init__(real, queries)
        self._run_id = run_id
        self._delete_dsn = delete_dsn
        self._deleted = False

    async def _delete_from_peer(self) -> None:
        import asyncpg

        peer = await asyncpg.connect(self._delete_dsn)
        try:
            await peer.execute("DELETE FROM runs WHERE id = $1", self._run_id)
        finally:
            await peer.close()

    async def fetchval(self, query: str, *args: Any) -> Any:
        if not self._deleted and "SELECT 1 FROM runs WHERE id = $1" in query:
            result = await super().fetchval(query, *args)
            if result is not None:
                await self._delete_from_peer()
                self._deleted = True
            return result
        return await super().fetchval(query, *args)

    async def fetchrow(self, query: str, *args: Any) -> Any:
        if not self._deleted and "FROM runs WHERE id = $1 FOR UPDATE" in query:
            await self._delete_from_peer()
            self._deleted = True
        return await super().fetchrow(query, *args)


class _DeleteRaceAcquire(_TraceAcquire):
    def __init__(self, real: Any, queries: list[str], run_id: str, delete_dsn: str) -> None:
        super().__init__(real, queries)
        self._run_id = run_id
        self._delete_dsn = delete_dsn

    async def __aenter__(self) -> _DeleteRaceConnection:
        return _DeleteRaceConnection(
            await self._real.__aenter__(), self._queries, self._run_id, self._delete_dsn
        )


class _DeleteRacePool(_TracePool):
    def __init__(self, real: Any, queries: list[str], run_id: str, delete_dsn: str) -> None:
        super().__init__(real, queries)
        self._run_id = run_id
        self._delete_dsn = delete_dsn

    def acquire(self, *args: Any, **kwargs: Any) -> _DeleteRaceAcquire:
        return _DeleteRaceAcquire(
            self._real.acquire(*args, **kwargs), self._queries, self._run_id, self._delete_dsn
        )


@needs_postgres
async def test_postgres_init_and_create_run_race(pg_dsn) -> None:
    from linch.run_stores.postgres import PostgresRunStore

    dsn = pg_dsn
    run_id = f"pg-init-race-{uuid4()}"
    stores = [PostgresRunStore(dsn, min_size=1, max_size=4) for _ in range(8)]
    try:
        rows = await asyncio.gather(
            *(
                store.create_run("session-race", id=run_id, meta={"winner": index})
                for index, store in enumerate(stores)
            ),
            return_exceptions=True,
        )
        assert sum(not isinstance(row, ValueError) for row in rows) == 1
        assert sum(isinstance(row, ValueError) for row in rows) == len(rows) - 1
    finally:
        await asyncio.gather(*(store.close() for store in stores))


@needs_postgres
async def test_postgres_create_pending_run_is_transactional_and_session_scoped(pg_dsn) -> None:
    from linch.run_stores.postgres import PostgresRunStore

    dsn = pg_dsn
    store = PostgresRunStore(dsn)
    run_id = f"pg-pending-{uuid4()}"
    try:
        pool = await store._ensure()
        async with pool.acquire() as conn:
            async with conn.transaction():
                created = await store.create_pending_run(
                    conn,
                    "session-pending",
                    id=run_id,
                    meta={"traceparent": "00-abc-def-01"},
                )
                assert created is not None
                assert created.status == "pending"
                assert await store.create_pending_run(conn, "session-pending", id=run_id) is None
                with pytest.raises(ValueError, match="belongs to session session-pending"):
                    await store.create_pending_run(conn, "other-session", id=run_id)
        loaded = await store.load_run(run_id)
        assert loaded is not None
        assert loaded.status == "pending"
    finally:
        await store.close()


@needs_postgres
async def test_postgres_leased_pending_adoption_skips_insert(pg_dsn) -> None:
    """An explicit token adopts a locked pending row without an INSERT race."""
    from linch.run_store import RunCheckpoint
    from linch.run_stores.postgres import PostgresRunStore
    from linch.types import Usage

    dsn = pg_dsn
    store = PostgresRunStore(dsn)
    run_id = f"pg-leased-adoption-no-insert-{uuid4()}"
    real_pool: Any = None
    try:
        real_pool = await store._ensure()
        async with real_pool.acquire() as conn:
            async with conn.transaction():
                await store.create_pending_run(conn, "session-pending", id=run_id)
        lease = await store.claim(run_id, "owner", 30.0)
        queries: list[str] = []
        store._pool = _TracePool(real_pool, queries)
        adopted = await store.create_run(
            "session-pending",
            id=run_id,
            initial_checkpoint=RunCheckpoint("started", "adopted", 1, Usage()),
            lease=lease,
        )
        assert adopted.status == "running"
        assert not any("INSERT INTO runs" in query for query in queries)
    finally:
        if real_pool is not None:
            store._pool = real_pool
        await store.close()


@needs_postgres
async def test_postgres_leased_adoption_race_does_not_recreate_pending_row(pg_dsn) -> None:
    """A row disappearing at the adoption boundary cannot be recreated."""
    from linch.run_store import LeaseLostError, RunCheckpoint
    from linch.run_stores.postgres import PostgresRunStore
    from linch.types import Usage

    dsn = pg_dsn
    store = PostgresRunStore(dsn)
    run_id = f"pg-leased-adoption-race-{uuid4()}"
    real_pool: Any = None
    try:
        real_pool = await store._ensure()
        async with real_pool.acquire() as conn:
            async with conn.transaction():
                await store.create_pending_run(conn, "session-pending", id=run_id)
        lease = await store.claim(run_id, "owner", 30.0)
        queries: list[str] = []
        store._pool = _DeleteRacePool(real_pool, queries, run_id, dsn)
        with pytest.raises(LeaseLostError):
            await store.create_run(
                "session-pending",
                id=run_id,
                initial_checkpoint=RunCheckpoint("started", "adopted", 1, Usage()),
                lease=lease,
            )
        store._pool = real_pool
        assert await store.load_run(run_id) is None
    finally:
        if real_pool is not None:
            store._pool = real_pool
        await store.close()


@needs_postgres
async def test_postgres_concurrent_snapshot_delete_and_prune(pg_dsn) -> None:
    from linch.run_stores.postgres import PostgresRunStore
    from linch.types import ProviderRequest

    dsn = pg_dsn
    store = PostgresRunStore(dsn)
    peer = PostgresRunStore(dsn)
    run_id = f"pg-snapshot-locks-{uuid4()}"
    try:
        await store.create_run("session-snapshots", id=run_id)
        request = ProviderRequest(model="test", system=[], tools=[], messages=[])
        snapshots = [await store.save(run_id, index, request) for index in range(1, 11)]
        await asyncio.wait_for(
            asyncio.gather(store.delete(snapshots[0].id, run_id=run_id), peer.prune(run_id)),
            timeout=5,
        )
        remaining = [await store.load(snapshot.id) for snapshot in snapshots]
        assert all(snapshot is None for snapshot in remaining)
    finally:
        await asyncio.gather(store.close(), peer.close())


@pytest.mark.parametrize(
    "failure_type",
    [RuntimeError, asyncio.CancelledError],
    ids=["commit-error", "cancelled"],
)
@needs_postgres
async def test_postgres_claim_rollback_does_not_publish_local_lease(
    failure_type: type[BaseException],
    pg_dsn,
) -> None:
    """A claim rolled back after UPDATE cannot poison the holder's local state."""
    from linch.run_store import LeaseLostError, RunCheckpoint, RunLease
    from linch.run_stores.postgres import PostgresRunStore
    from linch.types import Usage

    dsn = pg_dsn
    first = PostgresRunStore(dsn)
    peer = PostgresRunStore(dsn)
    run_id = f"pg-claim-rollback-{uuid4()}"
    real_pool: Any = None
    try:
        await first.create_run("session-claim-rollback", id=run_id)
        await first._ensure()
        real_pool = first._pool
        assert real_pool is not None
        first._pool = _RollbackPool(real_pool, failure_type)
        with pytest.raises(failure_type):
            await first.claim(run_id, "owner-a", 30.0)
        first._pool = real_pool

        assert not hasattr(first, "_held_leases")

        # A newly created run starts at epoch zero; the failed claimant's
        # candidate would have been epoch one, and the independent peer must
        # obtain that same epoch after rollback.
        failed_candidate_epoch = 1
        peer_lease = await peer.claim(run_id, "owner-b", 30.0)
        assert peer_lease.epoch == failed_candidate_epoch

        candidate = RunLease(
            run_id=run_id,
            owner="owner-a",
            epoch=failed_candidate_epoch,
            expires_at=peer_lease.expires_at,
            ttl_s=30.0,
        )
        with pytest.raises(LeaseLostError):
            await first.renew(candidate)
        with pytest.raises(LeaseLostError):
            await first.release(candidate)
        checkpoint = RunCheckpoint(
            phase="started",
            prompt="current owner",
            turn_index=1,
            total_usage=Usage(),
        )
        assert (await first.save_checkpoint(run_id, checkpoint, lease=peer_lease)).checkpoint

        renewed = await peer.renew(peer_lease)
        checkpoint = RunCheckpoint(
            phase="started",
            prompt="live owner",
            turn_index=1,
            total_usage=Usage(),
        )
        saved = await peer.save_checkpoint(run_id, checkpoint, lease=renewed)
        assert saved.checkpoint is not None
        assert saved.checkpoint.prompt == "live owner"
        await peer.release(renewed)
        assert not hasattr(peer, "_held_leases")
    finally:
        if real_pool is not None:
            first._pool = real_pool
        await asyncio.gather(first.close(), peer.close())


def _atomic_checkpoint(label: str = "before"):
    from linch.run_store import RunCheckpoint
    from linch.types import Usage

    return RunCheckpoint(phase="turn_complete", prompt=label, turn_index=1, total_usage=Usage())


def _atomic_event(label: str):
    from linch.events import UserEvent
    from linch.types import Message, TextBlock

    return UserEvent(message=Message(role="user", content=[TextBlock(text=label)]))


class _InspectConnection:
    def __init__(self, real: Any, pool: _InspectPool) -> None:
        self._real, self._pool = real, pool

    def transaction(self, *args: Any, **kwargs: Any) -> Any:
        self._pool.transactions += 1
        return self._real.transaction(*args, **kwargs)

    async def fetchrow(self, query: str, *args: Any) -> Any:
        if "UPDATE runs" in query and "checkpoint = $4" in query:
            if self._pool.before_update is not None:
                await self._pool.before_update()
            if self._pool.fail_update:
                raise self._pool.failure or OSError("checkpoint update failed")
        return await self._real.fetchrow(query, *args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class _InspectAcquire:
    def __init__(self, real: Any, pool: _InspectPool) -> None:
        self._real, self._pool = real, pool

    async def __aenter__(self) -> _InspectConnection:
        return _InspectConnection(await self._real.__aenter__(), self._pool)

    async def __aexit__(self, *args: Any) -> Any:
        return await self._real.__aexit__(*args)


class _InspectPool:
    def __init__(
        self,
        real: Any,
        *,
        before_update: Any = None,
        fail_update: bool = False,
        failure: BaseException | None = None,
    ):
        self.real = real
        self.before_update = before_update
        self.fail_update = fail_update or failure is not None
        self.failure = failure
        self.transactions = 0

    def acquire(self) -> _InspectAcquire:
        return _InspectAcquire(self.real.acquire(), self)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.real, name)


async def test_atomic_checkpoint_events_share_one_statement_and_visibility(pg_dsn) -> None:
    from linch.run_stores.postgres import PostgresRunStore

    store, peer = PostgresRunStore(pg_dsn), PostgresRunStore(pg_dsn)
    run_id = f"atomic-visible-{uuid4()}"
    real_pool = None
    try:
        await store.create_run("session", id=run_id)
        await store.save_checkpoint(run_id, _atomic_checkpoint())
        await peer.append_event(run_id, _atomic_event("peer"))
        real_pool = store._pool
        observations = []

        async def before_update() -> None:
            record = await peer.load_run(run_id)
            observations.append((record.checkpoint.prompt, len(await peer.load_events(run_id))))

        inspected = _InspectPool(real_pool, before_update=before_update)
        store._pool = inspected
        checkpoint = _atomic_checkpoint("after")
        record, seqs = await store.save_checkpoint_with_events(
            run_id,
            checkpoint,
            [_atomic_event("one"), _atomic_event("two")],
            status="completed",
        )
        assert inspected.transactions == 0
        assert observations == [("before", 1)]
        assert seqs == [2, 3]
        assert record.status == "completed"
        assert (await peer.load_run(run_id)).checkpoint.prompt == "after"
        assert [row.seq for row in await peer.load_events(run_id)] == [1, 2, 3]
        assert await peer.append_event(run_id, _atomic_event("next")) == 4
    finally:
        if real_pool is not None:
            store._pool = real_pool
        await asyncio.gather(store.close(), peer.close())


@pytest.mark.parametrize("failure", ["update", "commit", "cancelled"])
async def test_atomic_checkpoint_failure_rolls_back_events_and_checkpoint(pg_dsn, failure) -> None:
    from linch.run_stores.postgres import PostgresRunStore

    store = PostgresRunStore(pg_dsn)
    run_id = f"atomic-rollback-{uuid4()}"
    real_pool = None
    try:
        await store.create_run("session", id=run_id)
        await store.save_checkpoint(run_id, _atomic_checkpoint())
        await store.append_event(run_id, _atomic_event("before"))
        real_pool = store._pool
        failure_type = asyncio.CancelledError if failure == "cancelled" else RuntimeError
        if failure == "update":
            store._pool = _InspectPool(real_pool, failure=OSError("checkpoint update failed"))
            failure_type = OSError
        elif failure == "commit":
            store._pool = _InspectPool(real_pool, failure=RuntimeError("forced statement failure"))
        elif failure == "cancelled":
            store._pool = _InspectPool(real_pool, failure=asyncio.CancelledError())

        with pytest.raises(failure_type):
            await store.save_checkpoint_with_events(
                run_id,
                _atomic_checkpoint("after"),
                [_atomic_event("after")],
                status="completed",
            )
        store._pool = real_pool
        record = await store.load_run(run_id)
        assert record.status == "running"
        assert record.checkpoint.prompt == "before"
        assert [row.seq for row in await store.load_events(run_id)] == [1]
    finally:
        if real_pool is not None:
            store._pool = real_pool
        await store.close()


async def test_atomic_checkpoint_fencing_leaves_both_components_unchanged(pg_dsn) -> None:
    from linch.run_store import LeaseLostError
    from linch.run_stores.postgres import PostgresRunStore

    store, peer = PostgresRunStore(pg_dsn), PostgresRunStore(pg_dsn)
    run_id = f"atomic-fenced-{uuid4()}"
    try:
        await store.create_run("session", id=run_id)
        await store.save_checkpoint(run_id, _atomic_checkpoint())
        lease = await store.claim(run_id, "old", 0.01)
        await asyncio.sleep(max(0, lease.expires_at - time.time()) + 0.02)
        await peer.claim(run_id, "new", 30)
        with pytest.raises(LeaseLostError):
            await store.save_checkpoint_with_events(
                run_id,
                _atomic_checkpoint("zombie"),
                [_atomic_event("zombie")],
                lease=lease,
            )
        record = await peer.load_run(run_id)
        assert record.checkpoint.prompt == "before"
        assert await peer.load_events(run_id) == []
        assert not hasattr(store, "_held_leases")
    finally:
        await asyncio.gather(store.close(), peer.close())


@needs_postgres
async def test_missing_local_lease_owner_fails_closed(pg_dsn) -> None:
    from linch.run_store import LeaseLostError, RunLease
    from linch.run_stores.postgres import PostgresRunStore

    store = PostgresRunStore(pg_dsn)
    run_id = f"lease-owner-missing-{uuid4()}"
    try:
        await store.create_run("session", id=run_id)
        candidate = RunLease(
            run_id=run_id,
            owner="missing-owner",
            epoch=1,
            expires_at=time.time() + 30,
            ttl_s=30,
        )
        with pytest.raises(LeaseLostError):
            await store.save_checkpoint(run_id, _atomic_checkpoint(), lease=candidate)
        assert await store.load_run(run_id) is not None
    finally:
        await store.close()


@needs_postgres
async def test_postgres_pool_reuses_clean_connection_after_cancel_and_failed_write(pg_dsn) -> None:
    """Cancellation and a fenced failure must not poison the one-connection pool."""
    import asyncpg

    from linch.run_store import RunCheckpoint
    from linch.run_stores.postgres import PostgresRunStore
    from linch.types import Usage

    store = PostgresRunStore(pg_dsn, min_size=1, max_size=1)
    run_id = f"pool-reset-{uuid4()}"
    try:
        await store.create_run("session", id=run_id)
        pool = await store._ensure()
        async with pool.acquire() as conn:
            backend_pid = await conn.fetchval("SELECT pg_backend_pid()")

        async with pool.acquire() as conn:
            query = asyncio.create_task(conn.execute("SELECT pg_sleep(10)"))
            await asyncio.sleep(0.02)
            query.cancel()
            with pytest.raises(asyncio.CancelledError):
                await query

        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT pg_backend_pid()") == backend_pid

        async with pool.acquire() as conn:
            await conn.execute(
                "ALTER TABLE runs ADD CONSTRAINT wp8_failed_status_guard CHECK (status <> 'failed')"
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await store.mark_failed(run_id, _atomic_checkpoint("forced failure"))
        async with pool.acquire() as conn:
            await conn.execute("ALTER TABLE runs DROP CONSTRAINT wp8_failed_status_guard")

        checkpoint = RunCheckpoint(
            phase="turn_complete", prompt="after reset", turn_index=1, total_usage=Usage()
        )
        with pytest.raises(KeyError, match="run not found: missing"):
            await store.save_checkpoint("missing", checkpoint)
        await store.save_checkpoint(run_id, checkpoint)
        assert await store.append_event(run_id, _atomic_event("after reset")) == 1
        record = await store.load_run(run_id)
        assert record is not None
        assert record.checkpoint is not None
        assert record.checkpoint.prompt == "after reset"
        assert [event.seq for event in await store.load_events(run_id)] == [1]
    finally:
        await store.close()


async def test_pending_initial_checkpoint_adoption_transaction_rolls_back(pg_dsn):
    from linch.run_store import RunCheckpoint
    from linch.run_stores.postgres import PostgresRunStore
    from linch.types import Usage

    store = PostgresRunStore(pg_dsn)
    rid = f"initial-rollback-{uuid4()}"
    pool = await store._ensure()
    try:
        async with pool.acquire() as conn:
            await store.create_pending_run(conn, "s", id=rid, meta={"queue": "original"})
        lease = await store.claim(rid, "owner", 30)
        await pool.execute("""
            CREATE FUNCTION reject_initial_adoption() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.status = 'running' THEN RAISE EXCEPTION 'adoption rollback'; END IF;
                RETURN NEW;
            END $$;
            CREATE TRIGGER reject_initial_adoption AFTER UPDATE ON runs
            FOR EACH ROW EXECUTE FUNCTION reject_initial_adoption();
        """)
        with pytest.raises(Exception, match="adoption rollback"):
            await store.create_run(
                "s",
                id=rid,
                meta={"new": "metadata"},
                initial_checkpoint=RunCheckpoint("started", "hello", 0, Usage()),
            )
        record = await store.load_run(rid)
        assert record.status == "pending"
        assert record.checkpoint is None
        assert record.meta == {"queue": "original"}
        assert await store.load_events(rid) == []
        await pool.execute("DROP TRIGGER reject_initial_adoption ON runs")
        await store.release(lease)
    finally:
        await store.close()
