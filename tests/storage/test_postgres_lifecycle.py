"""Network-free lifecycle equivalence for all three Postgres store adapters."""

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
