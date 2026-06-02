from __future__ import annotations

from typing import Any, Protocol

from .types import MemoryItem, MemorySearchResult


class MemoryStore(Protocol):
    async def search(
        self,
        query: str,
        *,
        limit: int = 5,
        namespace: str | None = None,
        metadata_filter: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> list[MemorySearchResult]:
        """Return relevant memory items for *query*."""
        ...

    async def upsert(self, items: list[MemoryItem], **kwargs: Any) -> None:
        """Insert or replace memory items."""
        ...


def resolve_memory_store(value: Any) -> MemoryStore | None:
    if value is None:
        return None
    if hasattr(value, "search") and hasattr(value, "upsert"):
        return value
    if isinstance(value, dict):
        for key in ("memory_store", "memory"):
            candidate = value.get(key)
            if hasattr(candidate, "search") and hasattr(candidate, "upsert"):
                return candidate
    for attr in ("memory_store", "memory"):
        candidate = getattr(value, attr, None)
        if hasattr(candidate, "search") and hasattr(candidate, "upsert"):
            return candidate
    return None
