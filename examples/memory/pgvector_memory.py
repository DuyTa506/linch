"""pgvector memory store — recipe (NOT part of core Linch).

This file lives in examples/ deliberately.  Per Linch's design constraint
("No vector DB or embedding dependencies in core"), vector search adapters are
provided as recipes the host application customises, not as core library code.

What this shows
---------------
How to implement the two-method ``MemoryStore`` protocol using Postgres +
pgvector for semantic (embedding-based) search.  You supply the embedding
function — any model (OpenAI, Cohere, local sentence-transformers, etc.) works
as long as it returns a list of floats.

Requirements
------------
    pip install asyncpg pgvector

Your Postgres instance needs the pgvector extension::

    CREATE EXTENSION IF NOT EXISTS vector;

Usage
-----
    from examples.memory.pgvector_memory import PgVectorMemoryStore
    import openai

    async def embed(texts: list[str]) -> list[list[float]]:
        resp = await openai.AsyncOpenAI().embeddings.create(
            model="text-embedding-3-small", input=texts
        )
        return [d.embedding for d in resp.data]

    store = PgVectorMemoryStore(
        dsn="postgresql://user:pw@host/db",
        embed_fn=embed,
        dimensions=1536,
    )
    agent = Agent(..., deps=store)
    # MemorySearchTool and MemoryContextBuilder both call resolve_memory_store(ctx.deps)
    # which duck-types on .search/.upsert — so this store wires in transparently.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Coroutine
from typing import Any

# ── Optional-dependency check ─────────────────────────────────────────────────
# This recipe requires asyncpg + pgvector.  If they're absent we raise a clear
# error rather than an opaque ImportError at call time.

try:
    import asyncpg  # type: ignore[import-untyped]
except ModuleNotFoundError as exc:
    raise ModuleNotFoundError(
        "pgvector_memory requires asyncpg.  Install with: pip install asyncpg"
    ) from exc

try:
    from pgvector.asyncpg import register_vector  # type: ignore[import-untyped]
except ModuleNotFoundError as exc:
    raise ModuleNotFoundError(
        "pgvector_memory requires pgvector.  Install with: pip install pgvector"
    ) from exc

from linch.memory.types import MemoryItem, MemorySearchResult

# Embedding function type: accepts list[str], returns list[list[float]]
EmbedFn = Callable[[list[str]], Coroutine[Any, Any, list[list[float]]]]

_SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS vector_memories (
    namespace   TEXT NOT NULL,
    id          TEXT NOT NULL,
    content     TEXT NOT NULL,
    metadata    TEXT NOT NULL DEFAULT '{}',
    embedding   vector({dims}),
    created_at  DOUBLE PRECISION,
    updated_at  DOUBLE PRECISION,
    PRIMARY KEY (namespace, id)
);

CREATE INDEX IF NOT EXISTS vector_memories_embedding_idx
    ON vector_memories USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
"""


class PgVectorMemoryStore:
    """Semantic memory store using Postgres + pgvector.

    Implements the Linch ``MemoryStore`` protocol (``search`` + ``upsert``),
    so it works with :class:`~linch.memory.MemorySearchTool`,
    :class:`~linch.memory.MemoryUpsertTool`, and
    :class:`~linch.memory.MemoryContextBuilder` without any core changes.

    :param dsn: PostgreSQL connection string.
    :param embed_fn: Async function ``(texts: list[str]) -> list[list[float]]``.
    :param dimensions: Embedding dimensionality (must match your model).
    :param pool: Optional pre-created ``asyncpg.Pool``.
    :param min_size: Minimum pool connections.
    :param max_size: Maximum pool connections.
    """

    def __init__(
        self,
        dsn: str,
        embed_fn: EmbedFn,
        dimensions: int = 1536,
        *,
        pool: Any = None,
        min_size: int = 1,
        max_size: int = 10,
    ) -> None:
        self._dsn = dsn
        self._embed_fn = embed_fn
        self._dimensions = dimensions
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
            if self._pool is None:
                self._pool = await asyncpg.create_pool(
                    self._dsn,
                    min_size=self._min_size,
                    max_size=self._max_size,
                    init=register_vector,  # register vector type codec for each connection
                )
            async with self._pool.acquire() as conn:
                await conn.execute(_SCHEMA.format(dims=self._dimensions))
            self._initialized = True
        return self._pool

    # ── MemoryStore protocol ─────────────────────────────────────────────────

    async def upsert(self, items: list[MemoryItem], **kwargs: Any) -> None:
        if not items:
            return
        pool = await self._ensure()
        now = time.time()

        # Embed all content in one batch (more efficient than one-by-one)
        texts = [item.content for item in items]
        embeddings = await self._embed_fn(texts)

        async with pool.acquire() as conn:
            async with conn.transaction():
                for item, embedding in zip(items, embeddings, strict=True):
                    created_at = item.created_at if item.created_at is not None else now
                    item.created_at = created_at
                    item.updated_at = now
                    await conn.execute(
                        """
                        INSERT INTO vector_memories
                            (namespace, id, content, metadata, embedding, created_at, updated_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                        ON CONFLICT (namespace, id) DO UPDATE SET
                            content    = EXCLUDED.content,
                            metadata   = EXCLUDED.metadata,
                            embedding  = EXCLUDED.embedding,
                            updated_at = EXCLUDED.updated_at
                        """,
                        item.namespace or "",
                        item.id,
                        item.content,
                        json.dumps(item.metadata, sort_keys=True),
                        embedding,
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
        if limit <= 0:
            return []
        pool = await self._ensure()

        # Embed the query
        [query_embedding] = await self._embed_fn([query])

        async with pool.acquire() as conn:
            if namespace is None:
                rows = await conn.fetch(
                    """
                    SELECT id, content, metadata, namespace, created_at, updated_at,
                           1 - (embedding <=> $1::vector) AS score
                    FROM vector_memories
                    ORDER BY embedding <=> $1::vector
                    LIMIT $2
                    """,
                    query_embedding,
                    limit * 3,  # over-fetch to allow metadata filtering
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT id, content, metadata, namespace, created_at, updated_at,
                           1 - (embedding <=> $1::vector) AS score
                    FROM vector_memories
                    WHERE namespace = $2
                    ORDER BY embedding <=> $1::vector
                    LIMIT $3
                    """,
                    query_embedding,
                    namespace or "",
                    limit * 3,
                )

        results: list[MemorySearchResult] = []
        for row in rows:
            meta = json.loads(row["metadata"] or "{}")
            if metadata_filter:
                from linch.memory.keyword import _metadata_matches

                if not _metadata_matches(meta, metadata_filter):
                    continue
            item = MemoryItem(
                id=row["id"],
                content=row["content"],
                metadata=meta,
                namespace=row["namespace"] or None,
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            results.append(
                MemorySearchResult(
                    item=item,
                    score=float(row["score"]),
                    metadata={},
                )
            )
            if len(results) >= limit:
                break

        return results

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None


# ── Offline demo ──────────────────────────────────────────────────────────────


async def _offline_demo() -> None:
    """Show the API without a live database (no actual embedding/DB calls)."""
    import sys

    print("PgVectorMemoryStore — API preview (no live DB)")
    print()
    print("  store = PgVectorMemoryStore(")
    print('      dsn="postgresql://user:pw@host/db",')
    print("      embed_fn=my_async_embed_fn,")
    print("      dimensions=1536,")
    print("  )")
    print("  await store.upsert([MemoryItem(id='m1', content='...')])")
    print("  results = await store.search('my query', namespace='docs', limit=5)")
    print()
    print("  # Wires into Agent via deps:")
    print("  agent = Agent(..., deps=store)")
    print("  # MemorySearchTool / MemoryContextBuilder resolve via duck-typing")
    print()
    print("Requirements: pip install asyncpg pgvector")
    print("Postgres:     CREATE EXTENSION IF NOT EXISTS vector;")
    print()
    print("This recipe lives in examples/ (not core) by design — vector and")
    print("embedding dependencies stay out of the Linch package surface.")
    sys.exit(0)


if __name__ == "__main__":
    import asyncio

    asyncio.run(_offline_demo())
