"""MemoryStore extension template.

The MemoryStore seam is structural: implement ``search`` and ``upsert`` with
the same signatures, then pass the object to MemorySearchTool,
MemoryContextBuilder, hooks, or Agent deps.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from linch.memory import MemoryItem, MemorySearchResult


class TemplateMemoryStore:
    """Small in-memory MemoryStore suitable for adapting to a real backend."""

    def __init__(self) -> None:
        self._items: dict[tuple[str | None, str], MemoryItem] = {}
        self._lock = asyncio.Lock()

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
        terms = {part.lower() for part in query.split() if part}
        results: list[MemorySearchResult] = []
        async with self._lock:
            for item_namespace, _item_id in self._items:
                if namespace is not None and item_namespace != namespace:
                    continue
                item = self._items[(item_namespace, _item_id)]
                if not _metadata_matches(item.metadata, metadata_filter):
                    continue
                score = _score(terms, item.content)
                if score > 0 or not terms:
                    results.append(MemorySearchResult(item=item, score=score))
        results.sort(key=lambda result: result.score or 0, reverse=True)
        return results[:limit]

    async def upsert(self, items: list[MemoryItem], **kwargs: Any) -> None:
        del kwargs
        now = time.time()
        async with self._lock:
            for item in items:
                created_at = item.created_at or now
                self._items[(item.namespace, item.id)] = MemoryItem(
                    id=item.id,
                    content=item.content,
                    metadata=dict(item.metadata),
                    namespace=item.namespace,
                    created_at=created_at,
                    updated_at=now,
                )


def _score(terms: set[str], content: str) -> float:
    if not terms:
        return 1.0
    haystack = content.lower()
    matches = sum(1 for term in terms if term in haystack)
    return matches / len(terms)


def _metadata_matches(metadata: dict[str, Any], expected: dict[str, Any] | None) -> bool:
    if not expected:
        return True
    return all(metadata.get(key) == value for key, value in expected.items())
