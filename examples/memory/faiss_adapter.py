"""FAISS memory adapter recipe.

This example is intentionally outside core Linch. It adapts FAISS to the
existing ``MemoryStore`` shape (``search`` + ``upsert``) without adding FAISS or
embedding dependencies to the SDK.

Requirements:
    pip install faiss-cpu

You provide ``embed_fn``:
    async def embed_fn(texts: list[str]) -> list[list[float]]: ...
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable, Coroutine
from typing import Any

from linch.memory import MemoryItem, MemorySearchResult

try:
    import faiss  # type: ignore[import-untyped]
    import numpy as np  # type: ignore[import-untyped]
except ModuleNotFoundError as exc:
    raise ModuleNotFoundError(
        "faiss_adapter requires FAISS. Install with: pip install faiss-cpu"
    ) from exc

EmbedFn = Callable[[list[str]], Coroutine[Any, Any, list[list[float]]]]


class FaissMemoryStore:
    """Small FAISS-backed MemoryStore adapter.

    FAISS stores only vectors, so this adapter keeps item payloads and metadata
    in memory. For production, persist that side table in SQLite/Postgres/object
    storage and rebuild or snapshot the FAISS index.
    """

    def __init__(self, embed_fn: EmbedFn, *, dimensions: int) -> None:
        self._embed_fn = embed_fn
        self._dimensions = dimensions
        self._index = faiss.IndexFlatIP(dimensions)
        self._keys: list[tuple[str | None, str]] = []
        self._items: dict[tuple[str | None, str], MemoryItem] = {}
        self._vectors: dict[tuple[str | None, str], list[float]] = {}
        self._lock = asyncio.Lock()

    async def upsert(self, items: list[MemoryItem], **kwargs: Any) -> None:
        del kwargs
        if not items:
            return
        raw_vectors = await self._embed_fn([item.content for item in items])
        vectors = [_normalize(vector) for vector in raw_vectors]
        now = time.time()
        async with self._lock:
            for item, vector in zip(items, vectors, strict=True):
                key = (item.namespace, item.id)
                if key not in self._items:
                    self._keys.append(key)
                self._vectors[key] = vector
                self._items[key] = MemoryItem(
                    id=item.id,
                    content=item.content,
                    metadata=dict(item.metadata),
                    namespace=item.namespace,
                    created_at=item.created_at or now,
                    updated_at=now,
                )
            self._rebuild_index_locked()

    async def search(
        self,
        query: str,
        *,
        limit: int = 5,
        namespace: str | None = None,
        metadata_filter: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> list[MemorySearchResult]:
        del kwargs
        if limit <= 0:
            return []
        [query_vector] = await self._embed_fn([query])
        async with self._lock:
            if not self._keys:
                return []
            query_array = np.asarray([_normalize(query_vector)], dtype="float32")
            scores, indexes = self._index.search(query_array, min(len(self._keys), limit * 5))
            results: list[MemorySearchResult] = []
            for score, idx in zip(scores[0], indexes[0], strict=True):
                if idx < 0:
                    continue
                item = self._items[self._keys[idx]]
                if namespace is not None and item.namespace != namespace:
                    continue
                if not _metadata_matches(item.metadata, metadata_filter):
                    continue
                results.append(MemorySearchResult(item=item, score=float(score)))
                if len(results) >= limit:
                    break
            return results

    def _rebuild_index_locked(self) -> None:
        self._index = faiss.IndexFlatIP(self._dimensions)
        vectors = [self._vectors[key] for key in self._keys]
        if vectors:
            self._index.add(np.asarray(vectors, dtype="float32"))


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector] if norm else vector


def _metadata_matches(metadata: dict[str, Any], expected: dict[str, Any] | None) -> bool:
    if not expected:
        return True
    return all(metadata.get(key) == value for key, value in expected.items())


async def _demo() -> None:
    print("FaissMemoryStore is a recipe adapter.")
    print("Install dependency: pip install faiss-cpu")
    print("Pass an async embed_fn and use it wherever Linch accepts MemoryStore.")


if __name__ == "__main__":
    asyncio.run(_demo())
