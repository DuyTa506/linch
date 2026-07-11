"""Scalable reference memory (ROADMAP Phase 2.3).

The keyword stores were made scan-scalable without changing results:

- ``InMemoryKeywordMemoryStore`` partitions entries by namespace, yields to the
  event loop once per bounded chunk (not once per item), and selects a bounded
  top-k instead of sorting every match.
- ``SqliteMemoryStore`` / ``PostgresMemoryStore`` parse, filter, tokenize, score,
  and rank on the worker thread / blocking bridge — off the event loop.

These tests pin the observable contract: the retained rows, their descending
``(score, id)`` order, and the ``limit`` cut are identical to a naive full-scan
oracle; the in-memory yield is per-chunk; and SQLite scores off-loop.
"""

from __future__ import annotations

import random
import threading
from typing import Any

from linch.memory.keyword import (
    InMemoryKeywordMemoryStore,
    _metadata_matches,
    _tokenize,
)
from linch.memory.sqlite import SqliteMemoryStore
from linch.memory.types import MemoryItem

_WORDS = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta"]
_NAMESPACES: list[str | None] = [None, "a", "b", "c"]


def _naive_search_ids(
    items: list[MemoryItem],
    query: str,
    *,
    limit: int,
    namespace: str | None,
    metadata_filter: dict[str, Any] | None,
) -> list[str]:
    """Reference ranking: full scan, exact score, sort desc by (score, id)."""
    query_terms = _tokenize(query)
    if not query_terms or limit <= 0:
        return []
    scored: list[tuple[float, str]] = []
    for item in items:
        if namespace is not None and (item.namespace or "") != namespace:
            continue
        if metadata_filter and not _metadata_matches(item.metadata, metadata_filter):
            continue
        overlap = query_terms & _tokenize(item.content)
        if not overlap:
            continue
        scored.append((len(overlap) / len(query_terms), item.id))
    scored.sort(key=lambda pair: (pair[0], pair[1]), reverse=True)
    return [item_id for _, item_id in scored[:limit]]


def _random_items(rng: random.Random, n: int) -> list[MemoryItem]:
    items: list[MemoryItem] = []
    for i in range(n):
        content = " ".join(rng.sample(_WORDS, rng.randint(1, 4)))
        items.append(
            MemoryItem(
                id=f"item-{i:04d}",
                content=content,
                namespace=rng.choice(_NAMESPACES),
                metadata={"tag": rng.choice(["x", "y", "z"])},
            )
        )
    return items


async def test_in_memory_search_matches_naive_oracle_over_random_inputs() -> None:
    rng = random.Random(20260711)
    for _ in range(200):
        items = _random_items(rng, rng.randint(0, 40))
        store = InMemoryKeywordMemoryStore()
        await store.upsert(list(items))

        query = " ".join(rng.sample(_WORDS, rng.randint(1, 3)))
        namespace = rng.choice(_NAMESPACES)
        metadata_filter = rng.choice([None, {"tag": "x"}, {"tag": "q"}])
        limit = rng.randint(0, 6)

        got = await store.search(
            query, limit=limit, namespace=namespace, metadata_filter=metadata_filter
        )
        expected = _naive_search_ids(
            items, query, limit=limit, namespace=namespace, metadata_filter=metadata_filter
        )
        assert [r.item.id for r in got] == expected


async def test_sqlite_search_matches_naive_oracle_over_random_inputs() -> None:
    rng = random.Random(506)
    for _ in range(40):
        items = _random_items(rng, rng.randint(0, 30))
        async with SqliteMemoryStore(":memory:") as store:
            if items:
                await store.upsert(list(items))

            query = " ".join(rng.sample(_WORDS, rng.randint(1, 3)))
            namespace = rng.choice(_NAMESPACES)
            metadata_filter = rng.choice([None, {"tag": "y"}])
            limit = rng.randint(0, 6)

            got = await store.search(
                query, limit=limit, namespace=namespace, metadata_filter=metadata_filter
            )
        expected = _naive_search_ids(
            items, query, limit=limit, namespace=namespace, metadata_filter=metadata_filter
        )
        assert [r.item.id for r in got] == expected


async def test_search_result_carries_matched_terms_metadata() -> None:
    store = InMemoryKeywordMemoryStore()
    await store.upsert([MemoryItem(id="1", content="alpha beta gamma")])
    (result,) = await store.search("beta alpha zeta", limit=5)
    assert result.metadata == {"matched_terms": ["alpha", "beta"]}
    assert result.score == 2 / 3


async def test_bounded_top_k_returns_highest_scoring_within_limit() -> None:
    store = InMemoryKeywordMemoryStore()
    await store.upsert(
        [
            MemoryItem(id="all", content="alpha beta gamma"),  # score 1.0
            MemoryItem(id="two", content="alpha beta zeta"),  # score 2/3
            MemoryItem(id="one", content="alpha zeta eta"),  # score 1/3
        ]
    )
    results = await store.search("alpha beta gamma", limit=2)
    assert [r.item.id for r in results] == ["all", "two"]


async def test_namespace_scoped_search_ignores_other_partitions() -> None:
    store = InMemoryKeywordMemoryStore()
    await store.upsert(
        [
            MemoryItem(id="a1", content="alpha", namespace="a"),
            MemoryItem(id="b1", content="alpha", namespace="b"),
        ]
    )
    results = await store.search("alpha", namespace="a")
    assert [r.item.id for r in results] == ["a1"]


async def test_in_memory_search_yields_per_chunk_not_per_item(monkeypatch: Any) -> None:
    import linch.memory.keyword as kw

    real_sleep = kw.asyncio.sleep
    calls = 0

    async def counting_sleep(delay: float) -> None:
        nonlocal calls
        if delay == 0:
            calls += 1
        await real_sleep(delay)

    monkeypatch.setattr(kw.asyncio, "sleep", counting_sleep)

    store = InMemoryKeywordMemoryStore()
    n = kw._YIELD_CHUNK * 4
    await store.upsert([MemoryItem(id=f"i{i}", content="alpha") for i in range(n)])

    calls = 0  # ignore yields from upsert; measure only the scan
    await store.search("alpha", limit=5)

    # One yield per chunk (~n/_YIELD_CHUNK), nowhere near one-per-item (n).
    assert 1 <= calls <= (n // kw._YIELD_CHUNK) + 1
    assert calls < n // 10


async def test_sqlite_search_scores_off_the_event_loop(monkeypatch: Any) -> None:
    import linch.memory.sqlite as sqlite_mod
    from linch.memory.keyword import _score_rows as real_score_rows

    loop_thread = threading.get_ident()
    seen: dict[str, int] = {}

    def spy(rows: Any, query_terms: Any, metadata_filter: Any, limit: int) -> Any:
        seen["thread"] = threading.get_ident()
        return real_score_rows(rows, query_terms, metadata_filter, limit)

    monkeypatch.setattr(sqlite_mod, "_score_rows", spy)

    async with SqliteMemoryStore(":memory:") as store:
        await store.upsert([MemoryItem(id="1", content="alpha beta")])
        results = await store.search("alpha")

    assert [r.item.id for r in results] == ["1"]
    assert seen["thread"] != loop_thread
