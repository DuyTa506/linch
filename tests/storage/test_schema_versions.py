"""Built-in databases initialize only fresh or current-generation schemas."""

from __future__ import annotations

from importlib import import_module

import pytest

from linch._wire import UnsupportedWireVersionError

_PG = [
    ("linch.filesystem.postgres", "PostgresFileBackend", "filesystem", ("agentkit_files",)),
]


@pytest.fixture
async def pg_schema():
    import os
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
    from uuid import uuid4

    dsn = os.environ.get("AGENT_KIT_TEST_PG_DSN")
    if not dsn:
        pytest.skip("AGENT_KIT_TEST_PG_DSN not set")
    asyncpg = pytest.importorskip("asyncpg")
    conn = await asyncpg.connect(dsn)
    schema = f"linch_schema_{uuid4().hex}"
    await conn.execute(f'CREATE SCHEMA "{schema}"')
    parts = urlsplit(dsn)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key != "search_path"
    ]
    query.append(("search_path", schema))
    isolated_dsn = urlunsplit(parts._replace(query=urlencode(query)))
    isolated = await asyncpg.connect(isolated_dsn)
    try:
        yield isolated_dsn, isolated
    finally:
        await isolated.close()
        await conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await conn.close()


@pytest.mark.parametrize("module,name,key,tables", _PG, ids=[row[1] for row in _PG])
@pytest.mark.parametrize("version", [None, 0, 1, 2, 4, 999])
async def test_postgres_rejects_existing_schema_unchanged(
    pg_schema, module, name, key, tables, version
):
    dsn, conn = pg_schema
    await conn.execute(f"CREATE TABLE {tables[-1]} (old_value TEXT)")
    await conn.execute(f"INSERT INTO {tables[-1]} VALUES ('keep')")
    if version is not None:
        await conn.execute(
            "CREATE TABLE linch_schema_meta (store TEXT PRIMARY KEY, version INTEGER NOT NULL)"
        )
        await conn.execute("INSERT INTO linch_schema_meta VALUES ($1, $2)", key, version)
    store = getattr(import_module(module), name)(dsn)
    try:
        with pytest.raises(UnsupportedWireVersionError, match="start a new run"):
            await store._ensure()
        assert store._pool is None
        assert not store._initialized
    finally:
        await store.close()
    assert await conn.fetchval(f"SELECT old_value FROM {tables[-1]}") == "keep"
    rows = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")
    assert {row[0] for row in rows} == {tables[-1]} | (
        {"linch_schema_meta"} if version is not None else set()
    )
    if version is not None:
        assert (
            await conn.fetchval("SELECT version FROM linch_schema_meta WHERE store = $1", key)
            == version
        )


@pytest.mark.parametrize("version", [None])
async def test_postgres_rejects_tampered_metadata_on_reopen(pg_schema, version):
    from linch.run_stores.postgres import PostgresRunStore

    dsn, conn = pg_schema
    store = PostgresRunStore(dsn)
    run = await store.create_run("session", id="kept")
    await store.close()
    if version is None:
        await conn.execute("DELETE FROM linch_schema_meta WHERE store = 'runs'")
    else:
        await conn.execute(
            "UPDATE linch_schema_meta SET version = $1 WHERE store = 'runs'", version
        )
    store = PostgresRunStore(dsn)
    try:
        with pytest.raises(UnsupportedWireVersionError):
            await store.load_run(run.id)
    finally:
        await store.close()
    assert await conn.fetchval("SELECT id FROM runs") == "kept"
