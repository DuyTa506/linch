from __future__ import annotations

import asyncio
import re
import threading
import time
from typing import Any

from .types import MemoryItem, MemorySearchResult


class InMemoryKeywordMemoryStore:
    def __init__(self, items: list[MemoryItem] | None = None) -> None:
        self._items: dict[tuple[str, str], MemoryItem] = {}
        self._token_cache: dict[tuple[str, str], frozenset[str]] = {}
        self._lock = threading.RLock()
        for item in items or []:
            key = self._key(item)
            self._items[key] = item
            self._token_cache[key] = frozenset(_tokenize(item.content))

    async def upsert(self, items: list[MemoryItem], **kwargs: Any) -> None:
        now = time.time()
        for index, item in enumerate(items):
            if index:
                await asyncio.sleep(0)
            if item.created_at is None:
                item.created_at = now
            item.updated_at = now
            key = self._key(item)
            terms = frozenset(_tokenize(item.content))
            with self._lock:
                self._items[key] = item
                self._token_cache[key] = terms

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

        results: list[MemorySearchResult] = []
        with self._lock:
            snapshot = [
                (key, item, self._token_cache.get(key))
                for key, item in self._items.items()
            ]

        for index, (_key, item, cached_terms) in enumerate(snapshot):
            if index:
                await asyncio.sleep(0)
            if namespace is not None and item.namespace != namespace:
                continue
            if metadata_filter and not _metadata_matches(item.metadata, metadata_filter):
                continue
            item_terms = cached_terms or frozenset(_tokenize(item.content))
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

        results.sort(key=lambda result: (result.score or 0.0, result.item.id), reverse=True)
        return results[:limit]

    def list(self) -> list[MemoryItem]:
        with self._lock:
            return list(self._items.values())

    def _key(self, item: MemoryItem) -> tuple[str, str]:
        return (item.namespace or "", item.id)


def _tokenize(text: str) -> set[str]:
    return {match.group(0).lower() for match in re.finditer(r"\w+", text)}


def _metadata_matches(metadata: dict[str, Any], metadata_filter: dict[str, Any]) -> bool:
    for key, expected in metadata_filter.items():
        if metadata.get(key) != expected:
            return False
    return True
