"""Shared asyncpg import guard and Postgres pool lifecycle.

Used by all Postgres-backed store implementations.  Follows the repo's
established optional-dependency pattern: import guard at call-time, clear
error message pointing at the ``[postgres]`` extra.

Install::

    pip install 'linch[postgres]'
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast


def _import_asyncpg() -> Any:
    """Return the ``asyncpg`` module or raise with an install hint."""
    try:
        import asyncpg  # type: ignore[import-untyped]

        return asyncpg
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            "Postgres storage requires the optional 'asyncpg' dependency. "
            "Install with: pip install 'linch[postgres]'"
        ) from exc


async def ensure_pool(
    store: Any,
    schema: str,
    import_asyncpg: Callable[[], Any],
    *,
    advisory_lock_key: int | None = None,
    allow_missing_pool_on_error: bool = False,
) -> Any:
    """Lazily initialize a store's pool and schema under its existing lock.

    The store retains its lifecycle attributes and module-level driver import
    hook. Supplied pools follow the same cleanup policy as created pools.
    """
    if store._initialized:
        return store._pool
    async with store._init_lock:
        if store._initialized:
            return store._pool
        asyncpg = import_asyncpg()
        if store._pool is None:
            store._pool = await asyncpg.create_pool(
                store._dsn, min_size=store._min_size, max_size=store._max_size
            )
        try:
            async with store._pool.acquire() as conn:
                async with conn.transaction():
                    if advisory_lock_key is not None:
                        await conn.execute(
                            "SELECT pg_advisory_xact_lock($1::bigint)", advisory_lock_key
                        )
                    await conn.execute(schema)
        except Exception:
            pool, store._pool = store._pool, None
            if pool is not None or not allow_missing_pool_on_error:
                await cast(Any, pool).close()
            raise
        store._initialized = True
    return store._pool


async def close_pool(store: Any, *, reset_initialized: bool = False) -> None:
    """Close even supplied pools, retaining each adapter's reset policy."""
    if store._pool is not None:
        await store._pool.close()
        store._pool = None
        if reset_initialized:
            store._initialized = False
