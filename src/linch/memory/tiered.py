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
import inspect
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
        tier_limits: Optional per-tier search-limit overrides.  This is a HARD
            per-tier cap applied *before* the global merge: each tier is asked
            for at most ``tier_limits[tier]`` results, so a small per-tier limit
            can exclude globally higher-scoring items in that tier from the
            final ranking.  Leave it unset (the default) for a pure global
            top-N — when absent, every tier is queried with the full global
            ``limit`` passed to ``search()``, so the global merge sees all
            candidates and no item is dropped pre-merge.
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
        # Cache of each sub-store's accepted search kwargs, computed once per tier.
        # ``None`` names means "forward everything" (var-kwargs or unintrospectable).
        self._search_plans: dict[str, set[str] | None] = {}

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
                self._search_tier(
                    tier,
                    query,
                    limit=self._tier_limits.get(tier, limit),
                    namespace=namespace,
                    metadata_filter=metadata_filter,
                    extra_kwargs=kwargs,
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

    async def _search_tier(
        self,
        tier: str,
        query: str,
        *,
        limit: int,
        namespace: str | None,
        metadata_filter: dict[str, Any] | None,
        extra_kwargs: dict[str, Any],
    ) -> list[MemorySearchResult]:
        search = self._stores[tier].search
        kwargs: dict[str, Any] = {
            "limit": limit,
            "namespace": namespace,
            "metadata_filter": metadata_filter,
            **extra_kwargs,
        }
        accepted = self._search_param_names(tier, search)
        if accepted is None:
            # Sub-store accepts **kwargs (or its signature is unintrospectable):
            # forward everything.
            return await search(query, **kwargs)
        filtered = {key: value for key, value in kwargs.items() if key in accepted}
        return await search(query, **filtered)

    def _search_param_names(self, tier: str, search: Any) -> set[str] | None:
        """Return the set of keyword params *search* accepts, or ``None`` to forward all.

        The result is memoised per tier so ``inspect.signature`` runs at most once
        per sub-store rather than on every search call.
        """
        if tier in self._search_plans:
            return self._search_plans[tier]
        accepted: set[str] | None
        try:
            params = inspect.signature(search).parameters
        except (TypeError, ValueError):
            accepted = None
        else:
            if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
                accepted = None
            else:
                accepted = set(params)
        self._search_plans[tier] = accepted
        return accepted
