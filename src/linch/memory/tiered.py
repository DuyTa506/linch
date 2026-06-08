"""TieredMemoryStore — a composite MemoryStore that routes items to sub-stores by tier.

Tier is determined by ``item.metadata.get("tier")`` (one of ``"working"``,
``"episodic"``, or ``"semantic"``). Untagged items, and items with an
unrecognised tier value, default to the working tier.

This mirrors ``CompositeFileBackend`` (``filesystem/backend.py``) but routes by a
metadata marker rather than by path prefix.

Routing semantics:
- ``"working"``  — short-lived current-task context (default for untagged items).
- ``"episodic"`` — timestamped event log entries.
- ``"semantic"`` — distilled long-term facts.

The tier marker lives in ``MemoryItem.metadata`` because ``MemoryItem`` is
``slots=True`` and does not admit ad-hoc attributes.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .store import MemoryStore
from .types import MemoryItem, MemorySearchResult

_TIERS = ("working", "episodic", "semantic")
_DEFAULT_TIER = "working"


def _normalize_tier(value: Any) -> str:
    return value if isinstance(value, str) and value in _TIERS else _DEFAULT_TIER


class TieredMemoryStore:
    """Composite ``MemoryStore`` that partitions items across three tier sub-stores.

    Args:
        working:     Sub-store for the working (current-task) tier.
        episodic:    Sub-store for the episodic (event-log) tier.
        semantic:    Sub-store for the semantic (long-term facts) tier.
        tier_limits: Optional per-tier search-limit overrides.  When absent the
            global ``limit`` passed to ``search()`` is used for every tier.
    """

    def __init__(
        self,
        *,
        working: MemoryStore,
        episodic: MemoryStore,
        semantic: MemoryStore,
        tier_limits: dict[str, int] | None = None,
    ) -> None:
        self._stores: dict[str, MemoryStore] = {
            "working": working,
            "episodic": episodic,
            "semantic": semantic,
        }
        self._tier_limits: dict[str, int] = tier_limits or {}

    # ------------------------------------------------------------------
    # MemoryStore protocol
    # ------------------------------------------------------------------

    async def upsert(self, items: list[MemoryItem], **kwargs: Any) -> None:
        """Partition *items* by ``metadata["tier"]`` and delegate to the sub-store."""
        buckets: dict[str, list[MemoryItem]] = {tier: [] for tier in _TIERS}
        for item in items:
            tier = _normalize_tier(item.metadata.get("tier", _DEFAULT_TIER))
            buckets[tier].append(item)
        for tier, group in buckets.items():
            if group:
                await self._stores[tier].upsert(group, **kwargs)

    async def search(
        self,
        query: str,
        *,
        limit: int = 5,
        namespace: str | None = None,
        metadata_filter: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> list[MemorySearchResult]:
        """Fan out search across all tiers, merge, rank globally, return ``[:limit]``.

        Results from all three tiers are merged and re-sorted by the SDK's canonical
        ``(score, item.id)`` descending key (matching ``keyword.py`` / ``sqlite.py``).
        If the same ``(namespace, item.id)`` appears in multiple tiers, only the
        result with the higher score is kept.  A copy of ``result.metadata`` is
        made before stamping ``"tier"`` to avoid mutating the sub-store's
        internal dicts.
        """
        if limit <= 0:
            return []

        tier_results: list[list[MemorySearchResult]] = await asyncio.gather(
            *[
                self._stores[tier].search(
                    query,
                    limit=self._tier_limits.get(tier, limit),
                    namespace=namespace,
                    metadata_filter=metadata_filter,
                    **kwargs,
                )
                for tier in _TIERS
            ]
        )

        # Merge: dedup by namespace + item.id keeping the higher-scored result.
        seen: dict[tuple[str, str], MemorySearchResult] = {}
        for tier, results in zip(_TIERS, tier_results, strict=True):
            for result in results:
                item_key = (result.item.namespace or "", result.item.id)
                # Copy metadata before stamping so we don't mutate the sub-store's dict.
                stamped = MemorySearchResult(
                    item=result.item,
                    score=result.score,
                    metadata={**result.metadata, "tier": tier},
                )
                existing = seen.get(item_key)
                if existing is None or (result.score or 0.0) > (existing.score or 0.0):
                    seen[item_key] = stamped

        merged = list(seen.values())
        merged.sort(key=lambda r: (r.score or 0.0, r.item.id), reverse=True)
        return merged[:limit]
