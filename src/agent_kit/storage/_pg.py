"""asyncpg import guard and connection-pool factory.

Used by all Postgres-backed store implementations.  Follows the repo's
established optional-dependency pattern: import guard at call-time, clear
error message pointing at the ``[postgres]`` extra.

Install::

    pip install 'agent-kit[postgres]'
"""

from __future__ import annotations

from typing import Any


def _import_asyncpg() -> Any:
    """Return the ``asyncpg`` module or raise with an install hint."""
    try:
        import asyncpg  # type: ignore[import-untyped]

        return asyncpg
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            "Postgres storage requires the optional 'asyncpg' dependency. "
            "Install with: pip install 'agent-kit[postgres]'"
        ) from exc


async def make_pool(
    dsn: str,
    *,
    min_size: int = 1,
    max_size: int = 10,
) -> Any:
    """Create and return an ``asyncpg.Pool`` for *dsn*."""
    asyncpg = _import_asyncpg()
    return await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size)
