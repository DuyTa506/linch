"""PostgreSQL-backed :class:`~linch.filesystem.backend.FileBackend`.

Use for cross-session or cross-process persistence (e.g. shared offload
storage on multi-worker deployments).

Install::

    pip install 'linch[postgres]'

Usage::

    from linch.filesystem.postgres import PostgresFileBackend
    from linch.filesystem import OffloadConfig

    agent = Agent(
        ...,
        filesystem=PostgresFileBackend("postgresql://user:pw@host/db", namespace="tenant-1"),
        result_offload=OffloadConfig(),
    )
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from ..kernel.errors import ConfigError
from ..storage._pg import _import_asyncpg, close_pool, ensure_pool
from .backend import _slice_lines, normalize_path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agentkit_files (
    namespace   TEXT NOT NULL,
    path        TEXT NOT NULL,
    content     TEXT NOT NULL,
    updated_at  DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (namespace, path)
);
"""


def _escape_like(value: str) -> str:
    """Escape PostgreSQL LIKE metacharacters in a literal path prefix."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class PostgresFileBackend:
    """Virtual filesystem backed by a Postgres table.

    All six :class:`~linch.filesystem.backend.FileBackend` methods are
    implemented.  Connection pooling via ``asyncpg`` means concurrent
    read/write operations truly run in parallel.

    Files are keyed by ``(namespace, path)``: backends with different
    namespaces on one database never see each other's files.

    Args:
        dsn: PostgreSQL connection string.
        namespace: Non-empty owner scope (for example a tenant or session id)
            that every read, write, edit, delete, and listing is limited to.
        pool: Host-owned asyncpg pool to reuse; when set, dsn is ignored. The
            backend never closes this pool.
        min_size: Minimum pool connections.
        max_size: Maximum pool connections.

    Raises:
        ModuleNotFoundError: If the optional ``asyncpg`` dependency is unavailable.
    """

    def __init__(
        self,
        dsn: str,
        *,
        namespace: str,
        pool: Any = None,
        min_size: int = 1,
        max_size: int = 10,
    ) -> None:
        """Create a lazily initialized Postgres file backend.

        Args:
            dsn: PostgreSQL connection string, ignored when ``pool`` is supplied.
            namespace: Non-empty owner scope that all operations are limited to.
            pool: Optional host-owned asyncpg pool; the backend never closes it.
            min_size: Minimum size for a pool created from ``dsn``.
            max_size: Maximum size for a pool created from ``dsn``.

        Raises:
            ModuleNotFoundError: If ``asyncpg`` is not installed.
            ConfigError: If ``namespace`` is empty.
        """
        if not isinstance(namespace, str) or not namespace:
            raise ConfigError("PostgresFileBackend namespace must be a non-empty string")
        _import_asyncpg()
        self._namespace = namespace
        self._dsn = dsn
        self._pool: Any = pool
        self._owns_pool = pool is None
        self._min_size = min_size
        self._max_size = max_size
        self._initialized = False
        self._init_lock = asyncio.Lock()

    async def _ensure(self) -> Any:
        return await ensure_pool(
            self,
            _SCHEMA,
            _import_asyncpg,
            schema_key="filesystem",
            tables=("agentkit_files",),
            required_columns={"agentkit_files": ("namespace",)},
        )

    # ── FileBackend protocol ─────────────────────────────────────────────────

    async def read(self, path: str, *, offset: int = 0, limit: int | None = None) -> str:
        """Read a persisted file, optionally returning a line window.

        Args:
            path: Virtual path to read.
            offset: 1-indexed line to start at; ``0`` and ``1`` start at the first line.
            limit: Maximum number of lines; non-positive or ``None`` reads to the end.

        Returns:
            The selected file contents.

        Raises:
            FileNotFoundError: If ``path`` is not stored.
        """
        p = normalize_path(path)
        pool = await self._ensure()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT content FROM agentkit_files WHERE namespace = $1 AND path = $2",
                self._namespace,
                p,
            )
        if row is None:
            raise FileNotFoundError(p)
        return _slice_lines(row["content"], offset, limit)

    async def write(self, path: str, content: str) -> None:
        """Create or overwrite a persisted file.

        Args:
            path: Virtual path to create or replace.
            content: Complete text to store.
        """
        p = normalize_path(path)
        pool = await self._ensure()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO agentkit_files (namespace, path, content, updated_at)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (namespace, path) DO UPDATE SET
                    content    = EXCLUDED.content,
                    updated_at = EXCLUDED.updated_at
                """,
                self._namespace,
                p,
                content,
                time.time(),
            )

    async def ls(self, prefix: str = "") -> list[str]:
        """List persisted paths in sorted order.

        Args:
            prefix: Optional virtual path prefix; an empty prefix lists all files.

        Returns:
            Sorted paths matching ``prefix``.
        """
        pool = await self._ensure()
        async with pool.acquire() as conn:
            if not prefix:
                rows = await conn.fetch(
                    "SELECT path FROM agentkit_files WHERE namespace = $1 ORDER BY path",
                    self._namespace,
                )
                return [r["path"] for r in rows]
            pfx = normalize_path(prefix).rstrip("/")
            rows = await conn.fetch(
                "SELECT path FROM agentkit_files "
                "WHERE namespace = $1 AND (path = $2 OR path LIKE $3 ESCAPE '\\') "
                "ORDER BY path",
                self._namespace,
                pfx,
                _escape_like(pfx) + "/%",
            )
            return [r["path"] for r in rows]

    async def edit(self, path: str, old: str, new: str, *, replace_all: bool = False) -> int:
        """Replace exact text in a persisted file.

        Args:
            path: Virtual path whose contents should be edited.
            old: Exact text to find.
            new: Replacement text.
            replace_all: Whether to replace every occurrence instead of requiring a unique match.

        Returns:
            Number of replacements made.

        Raises:
            FileNotFoundError: If ``path`` is not stored.
            ValueError: If ``old`` is absent or is not unique while ``replace_all`` is false.
        """
        p = normalize_path(path)
        pool = await self._ensure()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT content FROM agentkit_files "
                    "WHERE namespace = $1 AND path = $2 FOR UPDATE",
                    self._namespace,
                    p,
                )
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
                    "UPDATE agentkit_files SET content=$1, updated_at=$2 "
                    "WHERE namespace=$3 AND path=$4",
                    updated,
                    time.time(),
                    self._namespace,
                    p,
                )
        return count if replace_all else 1

    async def exists(self, path: str) -> bool:
        """Check whether a persisted file exists.

        Args:
            path: Virtual path to check.

        Returns:
            ``True`` when a row exists for ``path``; otherwise ``False``.
        """
        p = normalize_path(path)
        pool = await self._ensure()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM agentkit_files WHERE namespace = $1 AND path = $2",
                self._namespace,
                p,
            )
        return row is not None

    async def delete(self, path: str) -> None:
        """Delete a persisted file if present.

        Args:
            path: Virtual path to remove.
        """
        p = normalize_path(path)
        pool = await self._ensure()
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM agentkit_files WHERE namespace = $1 AND path = $2",
                self._namespace,
                p,
            )

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def close(self) -> None:
        """Close the backend's owned Postgres pool.

        Caller-supplied pools are retained; their host owns their lifecycle.
        """
        await close_pool(self)
