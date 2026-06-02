"""PostgreSQL-backed :class:`~linch.memory.store.MemoryStore`.

Keyword-only search (same ``_tokenize``/``_metadata_matches`` as the SQLite
and in-memory stores).  No vector or embedding dependencies — per the
Linch design constraint, vector search lives in a recipe
(``examples/memory/pgvector_memory.py``), not in core.

Install::

    pip install 'linch[postgres]'

Usage::

    from linch.memory.postgres import PostgresMemoryStore

    store = PostgresMemoryStore("postgresql://user:pw@host/db")
    agent = Agent(..., deps=store)   # or pass to MemoryContextBuilder/MemorySearchTool
    ...
    await store.close()
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from ..storage._pg import _import_asyncpg
from .keyword import _metadata_matches, _tokenize
from .types import MemoryItem, MemorySearchResult

# Rows fetched from DB before Python-side scoring.  Keyword search requires
# in-process filtering, so we over-fetch then trim to `limit`.  Keep the cap
# generous enough to find the best matches but bounded to avoid full-table
# scans over the network.  Add a GIN/tsvector index and push the WHERE clause
# to Postgres when the store grows beyond a few thousand items.
_SEARCH_PREFETCH = 1000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    namespace   TEXT NOT NULL,
    id          TEXT NOT NULL,
    content     TEXT NOT NULL,
    metadata    TEXT NOT NULL DEFAULT '{}',
    created_at  DOUBLE PRECISION,
    updated_at  DOUBLE PRECISION,
    PRIMARY KEY (namespace, id)
);
"""


class PostgresMemoryStore:
    """Memory store backed by Postgres.

    :param dsn: PostgreSQL connection string.
    :param pool: Pass a pre-created ``asyncpg.Pool`` to reuse an existing pool.
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

    # ── MemoryStore protocol ─────────────────────────────────────────────────

    async def upsert(self, items: list[MemoryItem], **kwargs: Any) -> None:
        pool = await self._ensure()
        now = time.time()
        async with pool.acquire() as conn:
            async with conn.transaction():
                for item in items:
                    created_at = item.created_at if item.created_at is not None else now
                    item.created_at = created_at
                    item.updated_at = now
                    await conn.execute(
                        """
                        INSERT INTO memories
                            (namespace, id, content, metadata, created_at, updated_at)
                        VALUES ($1, $2, $3, $4, $5, $6)
                        ON CONFLICT (namespace, id) DO UPDATE SET
                            content    = EXCLUDED.content,
                            metadata   = EXCLUDED.metadata,
                            updated_at = EXCLUDED.updated_at
                        """,
                        item.namespace or "",
                        item.id,
                        item.content,
                        json.dumps(item.metadata, sort_keys=True),
                        created_at,
                        now,
                    )

    async def search(
        self,
        query: str,
        *,
        limit: int = 5,
        namespace: str | None = None,
        metadata_filter: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> list[MemorySearchResult]:
        query_terms = _tokenize(query)
        if not query_terms or limit <= 0:
            return []

        pool = await self._ensure()
        cap = max(_SEARCH_PREFETCH, limit * 20)
        async with pool.acquire() as conn:
            if namespace is None:
                rows = await conn.fetch(
                    "SELECT * FROM memories ORDER BY updated_at DESC LIMIT $1",
                    cap,
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM memories WHERE namespace = $1 "
                    "ORDER BY updated_at DESC LIMIT $2",
                    namespace or "",
                    cap,
                )

        results: list[MemorySearchResult] = []
        for row in rows:
            metadata = json.loads(row["metadata"] or "{}")
            if metadata_filter and not _metadata_matches(metadata, metadata_filter):
                continue
            item = MemoryItem(
                id=row["id"],
                content=row["content"],
                metadata=metadata,
                namespace=row["namespace"] or None,
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            item_terms = _tokenize(item.content)
            overlap = query_terms & item_terms
            if not overlap:
                continue
            score = len(overlap) / len(query_terms)
            results.append(
                MemorySearchResult(
                    item=item,
                    score=score,
                    metadata={"matched_terms": sorted(overlap)},
                )
            )

        results.sort(key=lambda r: (r.score or 0.0, r.item.id), reverse=True)
        return results[:limit]

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
