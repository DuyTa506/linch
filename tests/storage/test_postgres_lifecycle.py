"""Network-free lifecycle equivalence for all four Postgres store adapters."""

from __future__ import annotations

import asyncio
from importlib import import_module
from types import SimpleNamespace
from typing import Any

import pytest

_STORES = [
    ("linch.sessions.postgres", "PostgresSessionStore"),
    ("linch.filesystem.postgres", "PostgresFileBackend"),
    ("linch.run_stores.postgres", "PostgresRunStore"),
]


class _Context:
    def __init__(self, log: list[Any], name: str, value: Any) -> None:
        self.log, self.name, self.value = log, name, value

    async def __aenter__(self) -> Any:
        self.log.append(("enter", self.name))
        await asyncio.sleep(0)
        return self.value

    async def __aexit__(self, exc_type: Any, *_: Any) -> None:
        self.log.append(("exit", self.name, exc_type))


class _Pool:
    def __init__(self, *, fail_schema: bool = False) -> None:
        self.log: list[Any] = []
        self.fail_schema = fail_schema
        self.closed = 0

    def acquire(self) -> _Context:
        return _Context(self.log, "acquire", self)

    def transaction(self) -> _Context:
        return _Context(self.log, "transaction", self)

    async def execute(self, sql: str, *args: Any) -> None:
        self.log.append(("execute", sql, args))
        await asyncio.sleep(0)
        if self.fail_schema and "pg_advisory" not in sql:
            raise RuntimeError("schema failed")

    async def close(self) -> None:
        self.closed += 1


@pytest.fixture(params=_STORES, ids=[name for _, name in _STORES])
def adapter(request: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    module_name, name = request.param
    module = import_module(module_name)
    pools: list[_Pool] = []
    creations: list[Any] = []
    imports: list[bool] = []

    async def create_pool(dsn: str, **kwargs: Any) -> _Pool:
        creations.append((dsn, kwargs))
        pool = _Pool()
        pools.append(pool)
        await asyncio.sleep(0)
        return pool

    def import_driver() -> Any:
        imports.append(True)
        return SimpleNamespace(create_pool=create_pool)

    monkeypatch.setattr(module, "_import_asyncpg", import_driver)
    return SimpleNamespace(
        module=module,
        store_type=getattr(module, name),
        pools=pools,
        creations=creations,
        imports=imports,
        is_run=name == "PostgresRunStore",
    )


@pytest.mark.parametrize("supplied", [False, True])
async def test_lazy_initialization_is_locked_transactional_and_closes_supplied_pools(
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
    assert adapter.creations == ([] if supplied else [("test-dsn", {"min_size": 2, "max_size": 7})])
    expected = [("enter", "acquire"), ("enter", "transaction")]
    if adapter.is_run:
        expected.append(
            (
                "execute",
                "SELECT pg_advisory_xact_lock($1::bigint)",
                (adapter.module._SCHEMA_LOCK_KEY,),
            )
        )
    expected.extend(
        [
            ("execute", adapter.module._SCHEMA, ()),
            ("exit", "transaction", None),
            ("exit", "acquire", None),
        ]
    )
    assert pool.log == expected
    await store.close()
    await store.close()
    assert pool.closed == 1
    assert store._pool is None
    assert store._initialized is (not adapter.is_run)


async def test_schema_failure_closes_resets_and_retries_from_a_fresh_pool(adapter: Any) -> None:
    supplied = _Pool(fail_schema=True)
    store = adapter.store_type("test-dsn", pool=supplied)
    with pytest.raises(RuntimeError, match="schema failed"):
        await store._ensure()
    assert supplied.closed == 1
    assert store._pool is None
    assert store._initialized is False
    assert supplied.log[-2:] == [
        ("exit", "transaction", RuntimeError),
        ("exit", "acquire", RuntimeError),
    ]
    replacement = await store._ensure()
    assert replacement is adapter.pools[0]
    assert replacement is not supplied
    assert store._initialized is True
    await store.close()


async def test_close_failure_keeps_pool_and_initialized_state(adapter: Any) -> None:
    pool = _Pool()

    async def fail_close() -> None:
        raise RuntimeError("close failed")

    pool.close = fail_close  # type: ignore[method-assign]
    store = adapter.store_type("test-dsn", pool=pool)
    await store._ensure()
    with pytest.raises(RuntimeError, match="close failed"):
        await store.close()
    assert store._pool is pool
    assert store._initialized is True
