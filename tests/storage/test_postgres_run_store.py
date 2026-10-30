"""Local checks for the optional PostgreSQL run-store backend."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
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
            )
        )
        assert {row.id for row in rows} == {run_id}
        assert {row.session_id for row in rows} == {"session-race"}
    finally:
        await asyncio.gather(*(store.close() for store in stores))


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
