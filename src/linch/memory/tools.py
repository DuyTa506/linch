from __future__ import annotations

from typing import Any

from ..tools import Citation, ResourceAccess, ToolContext, ToolResult
from .store import MemoryStore, resolve_memory_store
from .types import MemoryItem


class MemorySearchTool:
    name = "SearchMemory"
    description = "Search memory for relevant facts."
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            "namespace": {"type": "string"},
        },
        "required": ["query"],
    }
    scope = "read"
    parallel = True
    tags = ("memory", "rag", "search")

    def __init__(
        self,
        store: MemoryStore | None = None,
        *,
        namespace: str | None = None,
        name: str | None = None,
    ) -> None:
        self.store = store
        self.namespace = namespace
        if name is not None:
            self.name = name

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        query = raw.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        limit = raw.get("limit", 5)
        try:
            limit_int = int(limit)
        except (TypeError, ValueError) as exc:
            raise ValueError("limit must be an integer") from exc
        namespace = raw.get("namespace", self.namespace)
        if namespace is not None and not isinstance(namespace, str):
            raise ValueError("namespace must be a string")
        return {
            "query": query.strip(),
            "limit": max(1, min(20, limit_int)),
            "namespace": namespace,
        }

    def resources(self, input: dict[str, Any]) -> list[ResourceAccess]:
        namespace = input.get("namespace") or self.namespace or "default"
        return [ResourceAccess(resource=f"memory:{namespace}", mode="read")]

    async def execute(self, input: dict[str, Any], ctx: ToolContext) -> ToolResult:
        store = self.store or resolve_memory_store(ctx.deps)
        if store is None:
            return ToolResult(
                content="Memory store is not configured.",
                summary="memory unavailable",
                is_error=True,
            )
        results = await store.search(
            input["query"],
            limit=input["limit"],
            namespace=input.get("namespace"),
        )
        tier_counts: dict[str, int] = {}
        citations = [
            Citation(
                id=result.item.id,
                source=f"memory:{result.item.namespace or input.get('namespace') or 'default'}",
                label=str(result.item.metadata.get("label", result.item.id)),
                chunk=result.item.content,
                score=result.score,
                metadata={**result.item.metadata, **result.metadata},
            )
            for result in results
        ]
        for citation in citations:
            tier = citation.metadata.get("tier")
            if isinstance(tier, str):
                tier_counts[tier] = tier_counts.get(tier, 0) + 1
        content = "\n".join(f"[{citation.id}] {citation.chunk}" for citation in citations)
        return ToolResult(
            content=content or "No memories found.",
            summary=f"{len(citations)} memory hit(s)",
            metadata={
                "query": input["query"],
                "namespace": input.get("namespace"),
                "result_ids": [citation.id for citation in citations],
                "tier_counts": tier_counts,
            },
            citations=citations,
            truncated=len(citations) >= input["limit"],
        )

    def summarize(self, input: dict[str, Any]) -> str:
        return f"SearchMemory({input.get('query', '?')})"


class MemoryUpsertTool:
    name = "UpsertMemory"
    description = "Store or replace a memory item."
    input_schema = {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "content": {"type": "string"},
            "label": {"type": "string"},
            "namespace": {"type": "string"},
            "metadata": {"type": "object"},
        },
        "required": ["id", "content"],
    }
    scope = "write"
    parallel = False
    tags = ("memory", "write")

    def __init__(
        self,
        store: MemoryStore | None = None,
        *,
        namespace: str | None = None,
        name: str | None = None,
    ) -> None:
        self.store = store
        self.namespace = namespace
        if name is not None:
            self.name = name

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        item_id = raw.get("id")
        content = raw.get("content", raw.get("text"))
        if not isinstance(item_id, str) or not item_id.strip():
            raise ValueError("id must be a non-empty string")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("content must be a non-empty string")
        namespace = raw.get("namespace", self.namespace)
        if namespace is not None and not isinstance(namespace, str):
            raise ValueError("namespace must be a string")
        metadata = raw.get("metadata")
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, dict):
            raise ValueError("metadata must be an object")
        label = raw.get("label")
        if isinstance(label, str) and label.strip():
            metadata = {**metadata, "label": label.strip()}
        return {
            "id": item_id.strip(),
            "content": content.strip(),
            "namespace": namespace,
            "metadata": metadata,
        }

    def resources(self, input: dict[str, Any]) -> list[ResourceAccess]:
        namespace = input.get("namespace") or self.namespace or "default"
        return [ResourceAccess(resource=f"memory:{namespace}", mode="write")]

    async def execute(self, input: dict[str, Any], ctx: ToolContext) -> ToolResult:
        store = self.store or resolve_memory_store(ctx.deps)
        if store is None:
            return ToolResult(
                content="Memory store is not configured.",
                summary="memory unavailable",
                is_error=True,
            )
        item = MemoryItem(
            id=input["id"],
            content=input["content"],
            metadata=input["metadata"],
            namespace=input.get("namespace"),
        )
        await store.upsert([item])
        return ToolResult(
            content=f"Stored memory {item.id}",
            summary="memory upsert",
            metadata={"id": item.id, "namespace": item.namespace},
        )

    def summarize(self, input: dict[str, Any]) -> str:
        return f"UpsertMemory({input.get('id', '?')})"
