from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..context import ContextBudget, ContextBuildResult, ContextBuildTurn
from ..types import Message, TextBlock
from .store import MemoryStore, resolve_memory_store
from .types import MemorySearchResult


class MemoryContextBuilder:
    def __init__(
        self,
        store: MemoryStore | None = None,
        *,
        limit: int = 5,
        namespace: str | None = None,
        max_tokens: int | None = None,
        query_builder: Callable[[ContextBuildTurn], str] | None = None,
        selected_tools: Any = None,
        title: str = "Retrieved memory",
        group_by_tier: bool = False,
    ) -> None:
        self.store = store
        self.limit = limit
        self.namespace = namespace
        self.max_tokens = max_tokens
        self.query_builder = query_builder
        self.selected_tools = selected_tools
        self.title = title
        self.group_by_tier = group_by_tier

    async def build(self, turn: ContextBuildTurn) -> ContextBuildResult:
        store = self.store or resolve_memory_store(turn.deps)
        query = self.query_builder(turn) if self.query_builder else latest_user_text(turn.messages)
        metadata: dict[str, Any] = {
            "memory": {
                "query": query,
                "namespace": self.namespace,
                "limit": self.limit,
                "hit_count": 0,
                "result_ids": [],
            }
        }
        if store is None or not query:
            return ContextBuildResult(
                selected_tools=self.selected_tools,
                budget=ContextBudget(max_tokens=self.max_tokens),
                metadata=metadata,
            )

        results = await store.search(query, limit=self.limit, namespace=self.namespace)
        metadata["memory"]["hit_count"] = len(results)
        metadata["memory"]["result_ids"] = [result.item.id for result in results]
        if not results:
            return ContextBuildResult(
                selected_tools=self.selected_tools,
                budget=ContextBudget(max_tokens=self.max_tokens),
                metadata=metadata,
            )

        if self.group_by_tier:
            content = format_memory_context_grouped(results, title=self.title)
        else:
            content = format_memory_context(results, title=self.title)
        return ContextBuildResult(
            messages=[Message(role="user", content=[TextBlock(text=content)])],
            selected_tools=self.selected_tools,
            budget=ContextBudget(max_tokens=self.max_tokens),
            metadata=metadata,
        )


def latest_user_text(messages: list[Message]) -> str:
    for message in reversed(messages):
        if message.role != "user":
            continue
        parts = []
        for block in message.content:
            if isinstance(block, TextBlock) and not block.text.startswith("<env>"):
                parts.append(block.text)
        text = "\n".join(parts).strip()
        if text:
            return text
    return ""


def format_memory_context(
    results: list[MemorySearchResult],
    *,
    title: str = "Retrieved memory",
) -> str:
    lines = [f"{title}:"]
    for index, result in enumerate(results, start=1):
        item = result.item
        label = item.metadata.get("label") or item.id
        score = f" score={result.score:.2f}" if result.score is not None else ""
        lines.append(f"[{index}] {label}{score}\n{item.content}")
    return "\n\n".join(lines)


def format_memory_context_grouped(
    results: list[MemorySearchResult],
    *,
    title: str = "Retrieved memory",
) -> str:
    """Format memory results grouped by tier.

    Produces one labelled section per tier that has at least one result.
    Tier order is: working → episodic → semantic.  Uses the ``"tier"`` key
    from ``result.metadata`` (stamped by ``TieredMemoryStore``); results
    without a tier key are placed in the working section.
    """
    _TIER_ORDER = ("working", "episodic", "semantic")
    by_tier: dict[str, list[MemorySearchResult]] = {}
    for result in results:
        tier = result.metadata.get("tier", "working")
        if not isinstance(tier, str) or tier not in _TIER_ORDER:
            tier = "working"
        by_tier.setdefault(tier, []).append(result)

    sections: list[str] = []
    for tier in _TIER_ORDER:
        group = by_tier.get(tier)
        if not group:
            continue
        lines = [f"{title} ({tier}):"]
        for index, result in enumerate(group, start=1):
            item = result.item
            label = item.metadata.get("label") or item.id
            score = f" score={result.score:.2f}" if result.score is not None else ""
            lines.append(f"[{index}] {label}{score}\n{item.content}")
        sections.append("\n\n".join(lines))
    return "\n\n".join(sections)
