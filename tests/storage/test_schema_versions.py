"""Built-in databases initialize only fresh or current-generation schemas."""

from __future__ import annotations

from importlib import import_module

import pytest

from linch._wire import UnsupportedWireVersionError

_PG = [
    (
        "linch.run_stores.postgres",
        "PostgresRunStore",
        "runs",
        ("runs", "run_events", "model_input_snapshots"),
    ),
    (
        "linch.sessions.postgres",
        "PostgresSessionStore",
        "sessions",
        (
            "sessions",
            "messages",
            "task_counters",
            "tasks",
            "task_edges",
            "session_snapshots",
            "session_inbox",
        ),
    ),
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
async def test_postgres_fresh_and_current_schema_reopens(pg_schema, module, name, key, tables):
    dsn, conn = pg_schema
    store_type = getattr(import_module(module), name)
    for _ in range(2):
        store = store_type(dsn)
        try:
            await store._ensure()
            assert (
                await conn.fetchval("SELECT version FROM linch_schema_meta WHERE store = $1", key)
                == 3
            )
            assert all([await conn.fetchval("SELECT to_regclass($1)", table) for table in tables])
            if key == "runs":
                assert await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = 'runs' AND column_name = 'last_event_seq')"
                )
            if key == "sessions":
                assert await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = 'sessions' AND column_name = 'last_message_seq')"
                )
        finally:
            await store.close()


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


async def test_postgres_different_stores_initialize_concurrently_and_reopen(pg_schema):
    import asyncio

    dsn, conn = pg_schema
    for _ in range(2):
        stores = [
            getattr(import_module(module), name)(dsn)
            for module, name, _, _ in _PG
            for _peer in range(2)
        ]
        try:
            await asyncio.gather(*(store._ensure() for store in stores))
            rows = await conn.fetch("SELECT store, version FROM linch_schema_meta")
            assert dict(rows) == {row[2]: 3 for row in _PG}
        finally:
            await asyncio.gather(*(store.close() for store in stores))


@pytest.mark.parametrize("version", [None, 0, 1, 2, 4, 999])
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


async def test_postgres_rejects_present_null_metadata_without_owned_tables(pg_schema):
    from linch.run_stores.postgres import PostgresRunStore

    dsn, conn = pg_schema
    await conn.execute("CREATE TABLE linch_schema_meta (store TEXT PRIMARY KEY, version INTEGER)")
    await conn.execute("INSERT INTO linch_schema_meta VALUES ('runs', NULL)")
    store = PostgresRunStore(dsn)
    try:
        with pytest.raises(UnsupportedWireVersionError):
            await store._ensure()
    finally:
        await store.close()
    assert await conn.fetchval("SELECT version FROM linch_schema_meta WHERE store = 'runs'") is None
    assert not await conn.fetchval("SELECT to_regclass('runs') IS NOT NULL")


async def test_postgres_rejects_current_metadata_with_old_counter_shape(pg_schema):
    from linch.run_stores.postgres import PostgresRunStore

    dsn, conn = pg_schema
    await conn.execute(
        "CREATE TABLE runs (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, status TEXT NOT NULL, "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, checkpoint TEXT, meta TEXT NOT NULL)"
    )
    await conn.execute(
        "CREATE TABLE linch_schema_meta (store TEXT PRIMARY KEY, version INTEGER NOT NULL)"
    )
    await conn.execute("INSERT INTO linch_schema_meta VALUES ('runs', 3)")
    store = PostgresRunStore(dsn)
    try:
        with pytest.raises(UnsupportedWireVersionError, match="start a new run"):
            await store._ensure()
    finally:
        await store.close()
    assert not await conn.fetchval("SELECT to_regclass('run_events') IS NOT NULL")
