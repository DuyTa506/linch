from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class MemoryItem:
    id: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    namespace: str | None = None
    created_at: float | None = None
    updated_at: float | None = None

    @property
    def text(self) -> str:
        return self.content


@dataclass(slots=True)
class MemorySearchResult:
    item: MemoryItem
    score: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
