"""PostgreSQL-backed :class:`~linch.filesystem.backend.FileBackend`.

Use instead of :class:`~linch.filesystem.backend.SqliteFileBackend` when
you need cross-session or cross-process persistence (e.g. shared offload
storage on multi-worker deployments).

Install::

    pip install 'linch[postgres]'

Usage::

    from linch.filesystem.postgres import PostgresFileBackend
    from linch.filesystem import OffloadConfig

    agent = Agent(
        ...,
        filesystem=PostgresFileBackend("postgresql://user:pw@host/db"),
        result_offload=OffloadConfig(),
    )
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from ..storage._pg import _import_asyncpg
from .backend import _slice_lines, normalize_path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agentkit_files (
    path        TEXT PRIMARY KEY,
    content     TEXT NOT NULL,
    updated_at  DOUBLE PRECISION NOT NULL
);
"""


class PostgresFileBackend:
    """Virtual filesystem backed by a Postgres table.

    All six :class:`~linch.filesystem.backend.FileBackend` methods are
    implemented.  Connection pooling via ``asyncpg`` means concurrent
    read/write operations truly run in parallel.

    :param dsn: PostgreSQL connection string.
    :param pool: Pass a pre-created ``asyncpg.Pool``.
    :param min_size: Minimum pool connections (default 1).
    :param max_size: Maximum pool connections (default 10).
    """

    def __init__(
        self,
        dsn: str,
        *,
        pool: Any = None,
        min_size: int = 1,
        max_size: int = 10,
    ) -> None:
        _import_asyncpg()
        self._dsn = dsn
        self._pool: Any = pool
        self._min_size = min_size
        self._max_size = max_size
        self._initialized = False
        self._init_lock = asyncio.Lock()

    async def _ensure(self) -> Any:
        if self._initialized:
            return self._pool
        async with self._init_lock:
            if self._initialized:
                return self._pool
            asyncpg = _import_asyncpg()
            if self._pool is None:
                self._pool = await asyncpg.create_pool(
                    self._dsn,
                    min_size=self._min_size,
                    max_size=self._max_size,
                )
            try:
                async with self._pool.acquire() as conn:
                    async with conn.transaction():
                        await conn.execute(_SCHEMA)
            except Exception:
                pool, self._pool = self._pool, None
                await pool.close()
                raise
            self._initialized = True
        return self._pool

    # ── FileBackend protocol ─────────────────────────────────────────────────

    async def read(self, path: str, *, offset: int = 0, limit: int | None = None) -> str:
        p = normalize_path(path)
        pool = await self._ensure()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT content FROM agentkit_files WHERE path = $1", p)
        if row is None:
            raise FileNotFoundError(p)
        return _slice_lines(row["content"], offset, limit)

    async def write(self, path: str, content: str) -> None:
        p = normalize_path(path)
        pool = await self._ensure()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO agentkit_files (path, content, updated_at)
                VALUES ($1, $2, $3)
                ON CONFLICT (path) DO UPDATE SET
                    content    = EXCLUDED.content,
                    updated_at = EXCLUDED.updated_at
                """,
                p,
                content,
                time.time(),
            )

    async def ls(self, prefix: str = "") -> list[str]:
        pool = await self._ensure()
        async with pool.acquire() as conn:
            if not prefix:
                rows = await conn.fetch("SELECT path FROM agentkit_files ORDER BY path")
                return [r["path"] for r in rows]
            pfx = normalize_path(prefix).rstrip("/")
            rows = await conn.fetch(
                "SELECT path FROM agentkit_files WHERE path = $1 OR path LIKE $2 ORDER BY path",
                pfx,
                pfx + "/%",
            )
            return [r["path"] for r in rows]

    async def edit(self, path: str, old: str, new: str, *, replace_all: bool = False) -> int:
        p = normalize_path(path)
        pool = await self._ensure()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow("SELECT content FROM agentkit_files WHERE path = $1", p)
                if row is None:
                    raise FileNotFoundError(p)
                text: str = row["content"]
                count = text.count(old)
                if count == 0:
                    raise ValueError(f"old string not found in {p}")
                if count > 1 and not replace_all:
                    raise ValueError(
                        f"old string is not unique in {p} ({count} matches); "
                        "pass replace_all=True or include more context"
                    )
                updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
                await conn.execute(
                    "UPDATE agentkit_files SET content=$1, updated_at=$2 WHERE path=$3",
                    updated,
                    time.time(),
                    p,
                )
        return count if replace_all else 1

    async def exists(self, path: str) -> bool:
        p = normalize_path(path)
        pool = await self._ensure()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT 1 FROM agentkit_files WHERE path = $1", p)
        return row is not None

    async def delete(self, path: str) -> None:
        p = normalize_path(path)
        pool = await self._ensure()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM agentkit_files WHERE path = $1", p)

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
