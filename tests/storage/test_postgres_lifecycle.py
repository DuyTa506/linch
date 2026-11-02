"""Network-free lifecycle equivalence for all three Postgres store adapters."""

from __future__ import annotations

import asyncio
from importlib import import_module
from types import SimpleNamespace
from typing import Any

import pytest

from linch.storage._pg import _SCHEMA_LOCK_KEY, _reset_connection

_STORES = [
    ("linch.sessions.postgres", "PostgresSessionStore"),
    ("linch.filesystem.postgres", "PostgresFileBackend"),
    ("linch.run_stores.postgres", "PostgresRunStore"),
]


class _Context:
    def __init__(
        self, log: list[Any], name: str, value: Any, error: Exception | None = None
    ) -> None:
        self.log, self.name, self.value = log, name, value
        self.error = error

    async def __aenter__(self) -> Any:
        self.log.append(("enter", self.name))
        await asyncio.sleep(0)
        if self.error is not None:
            raise self.error
        return self.value

    async def __aexit__(self, exc_type: Any, *_: Any) -> None:
        self.log.append(("exit", self.name, exc_type))


class _Pool:
    def __init__(self, *, fail_schema: bool = False, fail_acquire: bool = False) -> None:
        self.log: list[Any] = []
        self.fail_schema = fail_schema
        self.fail_acquire = fail_acquire
        self.closed = 0

    def acquire(self) -> _Context:
        if self.closed:
            raise RuntimeError("pool is closed")
        error = ConnectionResetError("acquire failed") if self.fail_acquire else None
        return _Context(self.log, "acquire", self, error)

    def transaction(self) -> _Context:
        return _Context(self.log, "transaction", self)

    async def execute(self, sql: str, *args: Any) -> None:
        self.log.append(("execute", sql, args))
        await asyncio.sleep(0)
        if self.fail_schema and "pg_advisory" not in sql:
            raise RuntimeError("schema failed")

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.log.append(("fetchval", sql, args))
        await asyncio.sleep(0)
        return False

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self.log.append(("fetchrow", sql, args))
        await asyncio.sleep(0)
        return None

    async def close(self) -> None:
        self.closed += 1


@pytest.fixture(params=_STORES, ids=[name for _, name in _STORES])
def adapter(request: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    module_name, name = request.param
    module = import_module(module_name)
    pools: list[_Pool] = []
    creations: list[Any] = []
    imports: list[bool] = []
    settings = SimpleNamespace(fail_schema=False)

    async def create_pool(dsn: str, **kwargs: Any) -> _Pool:
        creations.append((dsn, kwargs))
        pool = _Pool(fail_schema=settings.fail_schema)
        settings.fail_schema = False
        pools.append(pool)
        await asyncio.sleep(0)
        return pool

    def import_driver() -> Any:
        imports.append(True)
        return SimpleNamespace(
            create_pool=create_pool,
            UniqueViolationError=type("UniqueViolationError", (Exception,), {}),
        )

    monkeypatch.setattr(module, "_import_asyncpg", import_driver)
    return SimpleNamespace(
        module=module,
        store_type=getattr(module, name),
        pools=pools,
        creations=creations,
        imports=imports,
        settings=settings,
        is_run=name == "PostgresRunStore",
    )


@pytest.mark.parametrize("failure", ["acquire", "schema"])
async def test_borrowed_pool_survives_initialization_failure_and_retries(
    adapter: Any, failure: str
) -> None:
    pool = _Pool()
    store = adapter.store_type("", pool=pool)
    peer = adapter.store_type("", pool=pool)
    await peer._ensure()
    pool.fail_acquire = failure == "acquire"
    pool.fail_schema = failure == "schema"
    error = ConnectionResetError if failure == "acquire" else RuntimeError
    with pytest.raises(error, match=f"{failure} failed"):
        await store._ensure()

    assert pool.closed == 0
    assert store._pool is pool
    assert store._initialized is False
    pool.fail_acquire = pool.fail_schema = False
    async with (await peer._ensure()).acquire() as conn:
        await conn.execute("SELECT 1")
    assert await store._ensure() is pool
    assert adapter.creations == []
    await store.close()
    await peer.close()
    assert pool.closed == 0


async def test_closing_store_preserves_borrowed_pool_for_other_stores(adapter: Any) -> None:
    pool = _Pool()
    store = adapter.store_type("", pool=pool)
    peer = adapter.store_type("", pool=pool)
    await store._ensure()
    await peer._ensure()
    await store.close()
    assert pool.closed == 0
    assert store._pool is pool
    assert store._initialized is True
    async with (await peer._ensure()).acquire() as conn:
        await conn.execute("SELECT 1")
    await peer.close()
    assert pool.closed == 0
    assert adapter.creations == []


@pytest.mark.parametrize("supplied", [False, True])
async def test_lazy_initialization_is_locked_transactional_and_closes_only_owned_pools(
    adapter: Any, supplied: bool
) -> None:
    pool = _Pool() if supplied else None
    store = adapter.store_type("test-dsn", pool=pool, min_size=2, max_size=7)
    assert adapter.creations == []
    returned = await asyncio.gather(*(store._ensure() for _ in range(12)))
    pool = pool or adapter.pools[0]
    assert all(value is pool for value in returned)
    assert store._pool is pool
    assert store._initialized is True
    assert len(adapter.imports) == 2  # constructor and first initializer only
    if supplied:
        assert adapter.creations == []
    else:
        assert len(adapter.creations) == 1
        dsn, kwargs = adapter.creations[0]
        assert dsn == "test-dsn"
        assert kwargs["min_size"] == 2
        assert kwargs["max_size"] == 7
        assert kwargs["reset"] is _reset_connection
    assert pool.log[:3] == [
        ("enter", "acquire"),
        ("enter", "transaction"),
        ("execute", "SELECT pg_advisory_xact_lock($1::bigint)", (_SCHEMA_LOCK_KEY,)),
    ]
    locks = [row[2][0] for row in pool.log if row[0] == "execute" and "pg_advisory" in row[1]]
    assert locks == [_SCHEMA_LOCK_KEY] + (
        [adapter.module._SCHEMA_LOCK_KEY] if adapter.is_run else []
    )
    assert pool.log.count(("execute", adapter.module._SCHEMA, ())) == 1
    assert pool.log[-3][0] == "execute"
    assert "INSERT INTO linch_schema_meta" in pool.log[-3][1]
    assert pool.log[-3][2][1] == 3
    assert pool.log[-2:] == [("exit", "transaction", None), ("exit", "acquire", None)]
    await store.close()
    await store.close()
    assert pool.closed == (0 if supplied else 1)
    assert store._pool is (pool if supplied else None)
    assert store._initialized is (supplied or not adapter.is_run)


async def test_schema_failure_closes_resets_and_retries_from_a_fresh_pool(adapter: Any) -> None:
    adapter.settings.fail_schema = True
    store = adapter.store_type("test-dsn")
    with pytest.raises(RuntimeError, match="schema failed"):
        await store._ensure()
    failed = adapter.pools[0]
    assert failed.closed == 1
    assert store._pool is None
    assert store._initialized is False
    assert failed.log[-2:] == [
        ("exit", "transaction", RuntimeError),
        ("exit", "acquire", RuntimeError),
    ]
    replacement = await store._ensure()
    assert replacement is adapter.pools[1]
    assert replacement is not failed
    assert store._initialized is True
    await store.close()


async def test_close_failure_keeps_pool_and_initialized_state(adapter: Any) -> None:
    store = adapter.store_type("test-dsn")
    pool = await store._ensure()

    async def fail_close() -> None:
        raise RuntimeError("close failed")

    pool.close = fail_close  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="close failed"):
        await store.close()
    assert store._pool is pool
    assert store._initialized is True
