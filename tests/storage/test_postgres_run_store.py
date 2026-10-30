"""Local checks for the optional PostgreSQL run-store backend."""

from __future__ import annotations

import asyncio
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


@needs_postgres
async def test_postgres_init_and_create_run_race() -> None:
    from linch.run_stores.postgres import PostgresRunStore

    dsn = os.environ["AGENT_KIT_TEST_PG_DSN"]
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
async def test_postgres_create_pending_run_is_transactional_and_session_scoped() -> None:
    from linch.run_stores.postgres import PostgresRunStore

    dsn = os.environ["AGENT_KIT_TEST_PG_DSN"]
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
async def test_postgres_concurrent_snapshot_delete_and_prune() -> None:
    from linch.run_stores.postgres import PostgresRunStore
    from linch.types import ProviderRequest

    dsn = os.environ["AGENT_KIT_TEST_PG_DSN"]
    store = PostgresRunStore(dsn)
    peer = PostgresRunStore(dsn)
    run_id = f"pg-snapshot-locks-{uuid4()}"
    try:
        await store.create_run("session-snapshots", id=run_id)
        request = ProviderRequest(model="test", system=[], tools=[], messages=[])
        snapshots = [await store.save(run_id, index, request) for index in range(1, 11)]
        await asyncio.wait_for(
            asyncio.gather(store.delete(snapshots[0].id), peer.prune(run_id)), timeout=5
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
) -> None:
    """A claim rolled back after UPDATE cannot poison the holder's local state."""
    from linch.run_store import LeaseLostError, RunCheckpoint, RunLease
    from linch.run_stores.postgres import PostgresRunStore
    from linch.types import Usage

    dsn = os.environ["AGENT_KIT_TEST_PG_DSN"]
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

        assert first._held_leases == {}
        assert first._lease_ttls == {}

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
        )
        for token in (candidate, peer_lease):
            with pytest.raises(LeaseLostError):
                await first.renew(token)
            with pytest.raises(LeaseLostError):
                await first.release(token)
            assert first._held_leases == {}
            assert first._lease_ttls == {}

        renewed = await peer.renew(peer_lease)
        checkpoint = RunCheckpoint(
            phase="started",
            prompt="live owner",
            turn_index=1,
            total_usage=Usage(),
        )
        saved = await peer.save_checkpoint(run_id, checkpoint)
        assert saved.checkpoint is not None
        assert saved.checkpoint.prompt == "live owner"
        await peer.release(renewed)
        assert peer._held_leases == {}
        assert peer._lease_ttls == {}
    finally:
        if real_pool is not None:
            first._pool = real_pool
        await asyncio.gather(first.close(), peer.close())
