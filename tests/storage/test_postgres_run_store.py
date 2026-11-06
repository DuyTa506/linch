"""Local checks for the optional PostgreSQL run-store backend."""

from __future__ import annotations

import importlib.util
import os
import sys
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
