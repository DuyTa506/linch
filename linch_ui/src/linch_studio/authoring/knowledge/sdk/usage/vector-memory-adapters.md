# Vector Memory Adapters

Linch does not need a separate vector-store abstraction. The existing
`MemoryStore` shape is the adapter seam:

```python
class MemoryStore:
    async def search(
        self,
        query: str,
        *,
        limit: int = 5,
        namespace: str | None = None,
        metadata_filter: dict[str, object] | None = None,
        **kwargs,
    ) -> list[MemorySearchResult]: ...

    async def upsert(self, items: list[MemoryItem], **kwargs) -> None: ...
```

Vector DB dependencies stay in your application or in an adapter module under
`examples/`. Core Linch stays dependency-free and consumes only
`MemoryItem` / `MemorySearchResult`.

## Mapping

Map your vector backend rows to Linch memory primitives:

| Vector DB field | Linch field |
|---|---|
| primary key / point id | `MemoryItem.id` |
| document text / chunk text | `MemoryItem.content` |
| tenant / collection / user scope | `MemoryItem.namespace` |
| payload / metadata JSON | `MemoryItem.metadata` |
| similarity / distance | `MemorySearchResult.score` |

Use `namespace` for isolation such as tenant, user, workspace, corpus, or
project. Use `metadata_filter` for structured filters such as `tier`,
`document_id`, `source`, `language`, or `created_by`.

## Wiring

Any object with `search()` and `upsert()` works wherever Linch expects memory:

```python
from linch import Agent
from linch.hooks import ContextInjectionHook
from linch.memory import MemoryContextBuilder, MemorySearchTool, MemoryUpsertTool
from linch.tools.registry import empty_tools

store = MyVectorMemoryStore(...)

agent = Agent(
    ...,
    deps=store,
    hooks=[ContextInjectionHook(MemoryContextBuilder(namespace="docs", max_tokens=800))],
    tools=empty_tools(
        MemorySearchTool(namespace="docs"),
        MemoryUpsertTool(namespace="docs"),
    ),
)
```

`MemoryContextBuilder` recalls relevant items before each provider call and
injects them ephemerally. `MemorySearchTool` and `MemoryUpsertTool` let the
model query or write memory explicitly. Both resolve the store through
`Agent(deps=store)` or an object with a `memory_store` / `memory` attribute.

## Recipes

These recipes are examples, not core SDK dependencies:

| Backend | File | Install |
|---|---|---|
| FAISS | [`examples/memory/faiss_adapter.py`](../../examples/memory/faiss_adapter.py) | `pip install faiss-cpu` |
| pgvector | [`examples/memory/pgvector_memory.py`](../../examples/memory/pgvector_memory.py) | `pip install asyncpg pgvector` |
| Qdrant | [`examples/memory/qdrant_adapter.py`](../../examples/memory/qdrant_adapter.py) | `pip install qdrant-client` |

Each recipe asks you to provide an async embedding function:

```python
async def embed_fn(texts: list[str]) -> list[list[float]]:
    ...
```

That function can call OpenAI, Cohere, a local sentence-transformers model, or
any other embedding model. The adapter owns batching, vector upsert, vector
search, and mapping rows back to `MemorySearchResult`.

## Adapter Checklist

- Keep vector DB clients and embedding clients out of global state.
- Batch embeddings in `upsert()` when your provider supports it.
- Over-fetch before applying metadata filters if the backend cannot filter
  server-side.
- Preserve `namespace` on every item and include it in backend filters.
- Return scores in a consistent direction where larger means more relevant.
- Keep payload metadata JSON-serializable so it can surface in citations and
  run reports.
- Do not mutate `MemoryItem` inputs in-place unless your adapter documents it.

For local unit tests, keep using `InMemoryKeywordMemoryStore` or a tiny fake
adapter. Test your FAISS/pgvector/Qdrant adapters separately with their optional
dependencies installed.

