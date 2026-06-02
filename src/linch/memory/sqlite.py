from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from ..storage._executor import SqliteExecutor
from .keyword import _metadata_matches, _tokenize
from .types import MemoryItem, MemorySearchResult


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memories (
            namespace TEXT NOT NULL,
            id TEXT NOT NULL,
            content TEXT NOT NULL,
            metadata TEXT NOT NULL,
            created_at REAL,
            updated_at REAL,
            PRIMARY KEY (namespace, id)
        )
        """
    )


class SqliteMemoryStore:
    """Persistent memory store backed by SQLite.

    All database I/O runs on a single dedicated worker thread via
    :class:`~linch.storage._executor.SqliteExecutor`, so the asyncio event
    loop is never blocked.  Operations are serialised through that one thread —
    safe for concurrent use from multiple coroutines.

    Supports both async and sync context-manager protocols::

        async with SqliteMemoryStore(path) as store:
            await store.upsert([...])

        with SqliteMemoryStore(path) as store:
            ...   # close() called synchronously on __exit__
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._exec = SqliteExecutor(self.path, init=_init_schema, wal=True)

    # ── MemoryStore protocol ─────────────────────────────────────────────────

    async def upsert(self, items: list[MemoryItem], **kwargs: Any) -> None:
        now = time.time()
        rows: list[tuple[Any, ...]] = []
        for item in items:
            created_at = item.created_at if item.created_at is not None else now
            item.created_at = created_at
            item.updated_at = now
            rows.append(
                (
                    item.namespace or "",
                    item.id,
                    item.content,
                    json.dumps(item.metadata, sort_keys=True),
                    created_at,
                    now,
                )
            )
        await self._exec.run(lambda conn: _upsert(conn, rows))

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

        # Fetch rows off the event loop; score in Python on the event loop.
        raw_rows: list[dict[str, Any]] = await self._exec.run(
            lambda conn: _fetch_all(conn, namespace)
        )

        results: list[MemorySearchResult] = []
        for row in raw_rows:
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

    async def aclose(self) -> None:
        """Async close — preferred in async contexts."""
        await self._exec.close()

    def close(self) -> None:
        """Sync close — compatible with ``with`` context-manager."""
        self._exec.close_sync()

    # context-manager protocols (both sync and async)
    def __enter__(self) -> SqliteMemoryStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    async def __aenter__(self) -> SqliteMemoryStore:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()


# ── Sync helpers (worker thread) ─────────────────────────────────────────────


def _upsert(conn: sqlite3.Connection, rows: list[tuple[Any, ...]]) -> None:
    conn.executemany(
        """
        INSERT INTO memories(namespace, id, content, metadata, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(namespace, id) DO UPDATE SET
            content=excluded.content,
            metadata=excluded.metadata,
            updated_at=excluded.updated_at
        """,
        rows,
    )
    conn.commit()


def _fetch_all(conn: sqlite3.Connection, namespace: str | None) -> list[dict[str, Any]]:
    if namespace is None:
        cursor = conn.execute("SELECT * FROM memories")
    else:
        cursor = conn.execute(
            "SELECT * FROM memories WHERE namespace = ?",
            (namespace or "",),
        )
    return [dict(row) for row in cursor.fetchall()]
