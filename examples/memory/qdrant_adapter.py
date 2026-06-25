"""Qdrant memory adapter recipe.

This example adapts Qdrant to Linch's existing ``MemoryStore`` shape without
adding qdrant-client or embedding dependencies to core.

Requirements:
    pip install qdrant-client
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Coroutine
from typing import Any

from linch.memory import MemoryItem, MemorySearchResult

try:
    from qdrant_client import AsyncQdrantClient  # type: ignore[import-untyped]
    from qdrant_client.http import models as qdrant  # type: ignore[import-untyped]
except ModuleNotFoundError as exc:
    raise ModuleNotFoundError(
        "qdrant_adapter requires qdrant-client. Install with: pip install qdrant-client"
    ) from exc

EmbedFn = Callable[[list[str]], Coroutine[Any, Any, list[list[float]]]]


class QdrantMemoryStore:
    """Qdrant-backed MemoryStore adapter."""

    def __init__(
        self,
        client: AsyncQdrantClient,
        *,
        collection: str,
        embed_fn: EmbedFn,
        dimensions: int,
    ) -> None:
        self._client = client
        self._collection = collection
        self._embed_fn = embed_fn
        self._dimensions = dimensions

    async def ensure_collection(self) -> None:
        collections = await self._client.get_collections()
        names = {collection.name for collection in collections.collections}
        if self._collection in names:
            return
        await self._client.create_collection(
            collection_name=self._collection,
            vectors_config=qdrant.VectorParams(
                size=self._dimensions,
                distance=qdrant.Distance.COSINE,
            ),
        )

    async def upsert(self, items: list[MemoryItem], **kwargs: Any) -> None:
        del kwargs
        if not items:
            return
        await self.ensure_collection()
        vectors = await self._embed_fn([item.content for item in items])
        now = time.time()
        points = []
        for item, vector in zip(items, vectors, strict=True):
            payload = {
                "id": item.id,
                "content": item.content,
                "metadata": dict(item.metadata),
                "namespace": item.namespace,
                "created_at": item.created_at or now,
                "updated_at": now,
            }
            points.append(
                qdrant.PointStruct(
                    id=_point_id(item.namespace, item.id),
                    vector=vector,
                    payload=payload,
                )
            )
        await self._client.upsert(collection_name=self._collection, points=points)

    async def search(
        self,
        query: str,
        *,
        limit: int = 5,
        namespace: str | None = None,
        metadata_filter: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> list[MemorySearchResult]:
        del kwargs
        if limit <= 0:
            return []
        await self.ensure_collection()
        [query_vector] = await self._embed_fn([query])
        hits = await self._client.search(
            collection_name=self._collection,
            query_vector=query_vector,
            query_filter=_filter(namespace, metadata_filter),
            limit=limit,
            with_payload=True,
        )
        results: list[MemorySearchResult] = []
        for hit in hits:
            payload = hit.payload or {}
            item = MemoryItem(
                id=str(payload.get("id", hit.id)),
                content=str(payload.get("content", "")),
                metadata=dict(payload.get("metadata") or {}),
                namespace=(
                    payload.get("namespace") if isinstance(payload.get("namespace"), str) else None
                ),
                created_at=_float_or_none(payload.get("created_at")),
                updated_at=_float_or_none(payload.get("updated_at")),
            )
            results.append(MemorySearchResult(item=item, score=float(hit.score)))
        return results


def _point_id(namespace: str | None, item_id: str) -> str:
    return f"{namespace or ''}:{item_id}"


def _filter(namespace: str | None, metadata_filter: dict[str, Any] | None) -> Any:
    conditions = []
    if namespace is not None:
        conditions.append(
            qdrant.FieldCondition(key="namespace", match=qdrant.MatchValue(value=namespace))
        )
    for key, value in (metadata_filter or {}).items():
        conditions.append(
            qdrant.FieldCondition(key=f"metadata.{key}", match=qdrant.MatchValue(value=value))
        )
    return qdrant.Filter(must=conditions) if conditions else None


def _float_or_none(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) else None


async def _demo() -> None:
    print("QdrantMemoryStore is a recipe adapter.")
    print("Install dependency: pip install qdrant-client")
    print("Pass AsyncQdrantClient + async embed_fn and wire as a MemoryStore.")


if __name__ == "__main__":
    asyncio.run(_demo())
