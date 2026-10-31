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

from .._wire import WIRE_VERSION, require_wire_version

# All store types share metadata DDL, so lock before inspecting or creating it.
_SCHEMA_LOCK_KEY = 0x4C494E4348534348


async def _reset_connection(_connection: Any) -> None:
    """Keep asyncpg's internal rollback cleanup without the default reset SQL."""

    return None


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
    schema_key: str,
    tables: tuple[str, ...],
    required_columns: dict[str, tuple[str, ...]] | None = None,
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
                store._dsn,
                min_size=store._min_size,
                max_size=store._max_size,
                reset=_reset_connection,
            )
        try:
            async with store._pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute("SELECT pg_advisory_xact_lock($1::bigint)", _SCHEMA_LOCK_KEY)
                    if advisory_lock_key is not None:
                        await conn.execute(
                            "SELECT pg_advisory_xact_lock($1::bigint)", advisory_lock_key
                        )
                    meta_exists = await conn.fetchval(
                        "SELECT to_regclass('linch_schema_meta') IS NOT NULL"
                    )
                    metadata = None
                    if meta_exists:
                        metadata = await conn.fetchrow(
                            "SELECT version FROM linch_schema_meta WHERE store = $1", schema_key
                        )
                    if metadata is None:
                        for table in tables:
                            if await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", table):
                                require_wire_version(None)
                    else:
                        require_wire_version(metadata["version"])
                        for table, columns in (required_columns or {}).items():
                            present = await conn.fetchval(
                                """
                                SELECT count(*) = cardinality($2::text[])
                                FROM pg_attribute
                                WHERE attrelid = to_regclass($1::text)
                                  AND attnum > 0 AND NOT attisdropped
                                  AND attname = ANY($2::text[])
                                """,
                                table,
                                list(columns),
                            )
                            if not present:
                                require_wire_version(None)
                    await conn.execute(
                        "CREATE TABLE IF NOT EXISTS linch_schema_meta "
                        "(store TEXT PRIMARY KEY, version INTEGER NOT NULL)"
                    )
                    await conn.execute(schema)
                    await conn.execute(
                        "INSERT INTO linch_schema_meta (store, version) VALUES ($1, $2) "
                        "ON CONFLICT (store) DO NOTHING",
                        schema_key,
                        WIRE_VERSION,
                    )
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
