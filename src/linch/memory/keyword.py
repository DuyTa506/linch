from __future__ import annotations

import asyncio
import heapq
import json
import re
import threading
import time
from typing import Any

from .types import MemoryItem, MemorySearchResult

# Cooperative-yield granularity: the in-memory scan yields to the event loop once
# per this many candidates instead of once per item, so a 100k-entry namespace no
# longer pays 100k event-loop round-trips.
_YIELD_CHUNK = 512


class InMemoryKeywordMemoryStore:
    def __init__(self, items: list[MemoryItem] | None = None) -> None:
        # Entries are partitioned by namespace so a namespace-scoped search scans
        # only that namespace, not the whole corpus. Tokens are cached alongside.
        self._partitions: dict[str, dict[str, MemoryItem]] = {}
        self._token_partitions: dict[str, dict[str, frozenset[str]]] = {}
        self._lock = threading.RLock()
        for item in items or []:
            self._store(item, frozenset(_tokenize(item.content)))

    def _store(self, item: MemoryItem, terms: frozenset[str]) -> None:
        ns = item.namespace or ""
        self._partitions.setdefault(ns, {})[item.id] = item
        self._token_partitions.setdefault(ns, {})[item.id] = terms

    async def upsert(self, items: list[MemoryItem], **kwargs: Any) -> None:
        now = time.time()
        for index, item in enumerate(items):
            if index:
                await asyncio.sleep(0)
            if item.created_at is None:
                item.created_at = now
            item.updated_at = now
            terms = frozenset(_tokenize(item.content))
            with self._lock:
                self._store(item, terms)

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

        # Snapshot only the relevant partition(s) under the lock, then score
        # outside it so upserts are not blocked for the whole scan.
        with self._lock:
            if namespace is not None:
                part = self._partitions.get(namespace or "", {})
                tokens = self._token_partitions.get(namespace or "", {})
                snapshot = [(item, tokens.get(item_id)) for item_id, item in part.items()]
            else:
                snapshot = [
                    (item, self._token_partitions.get(ns, {}).get(item_id))
                    for ns, part in self._partitions.items()
                    for item_id, item in part.items()
                ]

        topk = _TopK(limit)
        for index, (item, cached_terms) in enumerate(snapshot):
            if index and index % _YIELD_CHUNK == 0:
                await asyncio.sleep(0)
            # namespace or "" partition can hold both None and "" namespaces;
            # keep the exact per-item check so those stay distinguishable.
            if namespace is not None and item.namespace != namespace:
                continue
            if metadata_filter and not _metadata_matches(item.metadata, metadata_filter):
                continue
            item_terms = cached_terms
            if item_terms is None:
                item_terms = frozenset(_tokenize(item.content))
            overlap = query_terms & item_terms
            if not overlap:
                continue
            topk.add(
                MemorySearchResult(
                    item=item,
                    score=len(overlap) / len(query_terms),
                    metadata={"matched_terms": sorted(overlap)},
                )
            )

        return topk.sorted()

    def list(self) -> list[MemoryItem]:
        with self._lock:
            return [item for part in self._partitions.values() for item in part.values()]

    def _key(self, item: MemoryItem) -> tuple[str, str]:
        return (item.namespace or "", item.id)


def _tokenize(text: str) -> set[str]:
    return {match.group(0).lower() for match in re.finditer(r"\w+", text)}


def _metadata_matches(metadata: dict[str, Any], metadata_filter: dict[str, Any]) -> bool:
    for key, expected in metadata_filter.items():
        if metadata.get(key) != expected:
            return False
    return True


class _TopK:
    """Online bounded top-k selector, descending by ``(score, id)``.

    Keeps at most ``limit`` results in a min-heap keyed on ``(score, id)`` as
    matches are added during the scan, so the full match list is never
    materialized. ``sorted()`` yields the same order as
    ``heapq.nlargest(limit, ..., key=(score, id))`` — descending by score, ties
    broken by id descending.
    """

    __slots__ = ("_limit", "_heap", "_counter")

    def __init__(self, limit: int) -> None:
        self._limit = limit
        # A per-add counter makes every heap entry's leading tuple unique, so the
        # MemorySearchResult payload is never reached by tuple comparison.
        self._counter = 0
        self._heap: list[tuple[float, str, int, MemorySearchResult]] = []

    def add(self, result: MemorySearchResult) -> None:
        if self._limit <= 0:
            return
        entry = (result.score or 0.0, result.item.id, self._counter, result)
        self._counter += 1
        if len(self._heap) < self._limit:
            heapq.heappush(self._heap, entry)
        else:
            heapq.heappushpop(self._heap, entry)

    def sorted(self) -> list[MemorySearchResult]:
        return [entry[3] for entry in sorted(self._heap, reverse=True)]


def _score_rows(
    rows: Any,
    query_terms: set[str],
    metadata_filter: dict[str, Any] | None,
    limit: int,
) -> list[MemorySearchResult]:
    """Parse, filter, tokenize, score, and rank raw memory rows.

    Pure and CPU-only (no I/O), so persistent stores can run it off the event
    loop. Each ``row`` is any mapping with ``id``/``content``/``metadata``/
    ``namespace``/``created_at``/``updated_at`` keys (sqlite3.Row, asyncpg
    Record, or dict).
    """
    topk = _TopK(limit)
    for row in rows:
        raw_meta = row["metadata"]
        if isinstance(raw_meta, str) and raw_meta:
            metadata = json.loads(raw_meta)
        else:
            metadata = raw_meta or {}
        if metadata_filter and not _metadata_matches(metadata, metadata_filter):
            continue
        item_terms = _tokenize(row["content"])
        overlap = query_terms & item_terms
        if not overlap:
            continue
        topk.add(
            MemorySearchResult(
                item=MemoryItem(
                    id=row["id"],
                    content=row["content"],
                    metadata=metadata,
                    namespace=row["namespace"] or None,
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                ),
                score=len(overlap) / len(query_terms),
                metadata={"matched_terms": sorted(overlap)},
            )
        )
    return topk.sorted()
