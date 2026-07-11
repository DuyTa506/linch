"""Online bounded top-k heap (ROADMAP WS3 — memory scan).

``_TopK`` replaces the "append every match then ``heapq.nlargest``" pattern with
an incremental bounded heap maintained during the scan, so the full match list
is never materialized. These tests pin the two properties callers rely on: the
retained/ordered results are identical to the naive
``sorted(..., key=(score, id), reverse=True)[:limit]`` oracle (including tie
ordering by id, descending), and heap occupancy never exceeds ``limit``.
"""

from __future__ import annotations

import random

from linch.memory.keyword import _TopK
from linch.memory.types import MemoryItem, MemorySearchResult


def _result(item_id: str, score: float | None) -> MemorySearchResult:
    return MemorySearchResult(item=MemoryItem(id=item_id, content=""), score=score)


def _oracle(results: list[MemorySearchResult], limit: int) -> list[str]:
    ranked = sorted(results, key=lambda r: (r.score or 0.0, r.item.id), reverse=True)
    return [r.item.id for r in ranked[: max(limit, 0)]]


def test_topk_matches_sorted_oracle_over_random_inputs() -> None:
    rng = random.Random(20260711)
    for _ in range(500):
        n = rng.randint(0, 40)
        limit = rng.randint(0, 8)
        results = [_result(f"id-{i:03d}", rng.choice([0.0, 0.25, 0.5, 0.5, 1.0])) for i in range(n)]
        rng.shuffle(results)
        topk = _TopK(limit)
        for r in results:
            topk.add(r)
        assert [r.item.id for r in topk.sorted()] == _oracle(results, limit)


def test_topk_occupancy_never_exceeds_limit() -> None:
    rng = random.Random(7)
    limit = 5
    topk = _TopK(limit)
    for i in range(200):
        topk.add(_result(f"id-{i:04d}", rng.random()))
        assert len(topk._heap) <= limit


def test_topk_ties_broken_by_id_descending() -> None:
    topk = _TopK(2)
    for item_id in ["a", "c", "b"]:
        topk.add(_result(item_id, 0.5))
    assert [r.item.id for r in topk.sorted()] == ["c", "b"]


def test_topk_zero_and_negative_limit_return_nothing() -> None:
    for limit in (0, -1):
        topk = _TopK(limit)
        topk.add(_result("a", 1.0))
        assert topk.sorted() == []


def test_topk_handles_none_score_as_zero() -> None:
    topk = _TopK(3)
    topk.add(_result("a", None))
    topk.add(_result("b", 0.5))
    topk.add(_result("c", None))
    # None scores rank as 0.0; among the two zero-scored, id descending -> c, a.
    assert [r.item.id for r in topk.sorted()] == ["b", "c", "a"]
