"""RAG tools — hybrid_search, keyword_search, graph_search, web_search.

All four are read-only (scope="read", parallel_safe=True) so AgentKit will
run them concurrently when the model calls multiple tools in one turn.

Clients (vector store, graph DB, web search API) are injected via
Agent(deps=...) and accessed as ctx.deps inside execute(). This avoids
__init__ closures and makes swapping backends in tests trivial.

Run:
    ANTHROPIC_API_KEY=sk-... python examples/tools/rag_tools.py
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

from agent_kit import Agent
from agent_kit.config import FeatureFlags, SystemPromptConfig
from agent_kit.providers.anthropic import AnthropicProvider, AnthropicProviderOptions
from agent_kit.sessions import InMemorySessionStore
from agent_kit.tools.base import Citation, ToolContext, ToolResult
from agent_kit.tools.registry import ToolRegistry

# ── Deps container ────────────────────────────────────────────────────────────
#
# In production each field would be a real client (e.g. pgvector, neo4j, Tavily).
# Here we use in-memory stubs so the example runs without external services.


@dataclass
class RagDeps:
    vector_store: object  # implements async search(query, top_k) -> list[dict]
    graph_db: object  # implements async query(entity, hops) -> list[dict]
    keyword_index: object  # implements async search(query, top_k) -> list[dict]
    web_client: object  # implements async search(query, top_k) -> list[dict]


# ── HybridSearchTool ──────────────────────────────────────────────────────────
#
# Merges dense (vector) + sparse (keyword) hits, deduplicates, and re-ranks by
# score. This is the most common pattern in production RAG pipelines.


class HybridSearchTool:
    name = "hybrid_search"
    description = (
        "Search the knowledge base using both semantic (vector) and keyword "
        "matching, then merge and rank results by relevance score."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Natural-language search query"},
            "top_k": {
                "type": "integer",
                "default": 5,
                "minimum": 1,
                "maximum": 20,
                "description": "Number of results to return after merging",
            },
            "alpha": {
                "type": "number",
                "default": 0.5,
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "Weight for vector score (1-alpha goes to keyword score)",
            },
        },
        "required": ["query"],
    }
    scope = "read"
    parallel_safe = True  # safe to run concurrently with other read tools
    tags = {"rag", "search"}

    def validate(self, raw: dict) -> dict:
        if not raw.get("query"):
            raise ValueError("query is required")
        return {
            "query": str(raw["query"]),
            "top_k": int(raw.get("top_k", 5)),
            "alpha": float(raw.get("alpha", 0.5)),
        }

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        deps: RagDeps = ctx.deps
        query, top_k, alpha = input["query"], input["top_k"], input["alpha"]

        # Run both searches concurrently — they're independent.
        vector_hits, keyword_hits = await asyncio.gather(
            deps.vector_store.search(query, top_k=top_k),
            deps.keyword_index.search(query, top_k=top_k),
        )

        # Merge: combine scores with alpha weighting, dedup by doc id.
        merged: dict[str, dict] = {}
        for hit in vector_hits:
            merged[hit["id"]] = {**hit, "score": hit["score"] * alpha}
        for hit in keyword_hits:
            if hit["id"] in merged:
                merged[hit["id"]]["score"] += hit["score"] * (1 - alpha)
            else:
                merged[hit["id"]] = {**hit, "score": hit["score"] * (1 - alpha)}

        results = sorted(merged.values(), key=lambda h: h["score"], reverse=True)[:top_k]

        citations = [
            Citation(
                id=r["id"],
                source=r.get("source", r["id"]),
                label=r.get("title"),
                chunk=r.get("text"),
                score=r["score"],
            )
            for r in results
        ]
        content = (
            "\n\n".join(
                f"[{r['id']}] (score={r['score']:.3f})\n{r.get('text', '')}" for r in results
            )
            or "No results found."
        )

        return ToolResult(
            content=content,
            summary=f"hybrid_search({query[:40]!r}) → {len(results)} hits",
            citations=citations,
        )

    def summarize(self, input: dict) -> str:
        return f"hybrid_search({input.get('query', '?')[:40]!r})"


# ── KeywordSearchTool ─────────────────────────────────────────────────────────
#
# Pure BM25 / full-text search. Faster than vector search and better for
# exact-match lookups (product codes, names, technical terms).


class KeywordSearchTool:
    name = "keyword_search"
    description = (
        "Search the knowledge base using BM25 full-text matching. "
        "Best for exact terms, product codes, and proper nouns."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Keywords or phrase to search for"},
            "top_k": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20},
            "filter": {
                "type": "object",
                "description": 'Optional metadata filters, e.g. {"category": "docs"}',
                "additionalProperties": {"type": "string"},
            },
        },
        "required": ["query"],
    }
    scope = "read"
    parallel_safe = True
    tags = {"rag", "search"}

    def validate(self, raw: dict) -> dict:
        if not raw.get("query"):
            raise ValueError("query is required")
        return {
            "query": str(raw["query"]),
            "top_k": int(raw.get("top_k", 5)),
            "filter": dict(raw["filter"]) if raw.get("filter") else {},
        }

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        deps: RagDeps = ctx.deps
        results = await deps.keyword_index.search(
            input["query"], top_k=input["top_k"], filter=input["filter"]
        )
        citations = [
            Citation(
                id=r["id"],
                source=r.get("source", r["id"]),
                chunk=r.get("text"),
                score=r.get("score"),
            )
            for r in results
        ]
        content = (
            "\n\n".join(f"[{r['id']}]\n{r.get('text', '')}" for r in results) or "No results found."
        )
        return ToolResult(
            content=content,
            summary=f"keyword_search({input['query'][:40]!r}) → {len(results)} hits",
            citations=citations,
        )

    def summarize(self, input: dict) -> str:
        return f"keyword_search({input.get('query', '?')[:40]!r})"


# ── GraphSearchTool ───────────────────────────────────────────────────────────
#
# Traverses a knowledge graph starting from an entity. Good for relationship
# questions: "what products use component X?", "who reports to person Y?".


class GraphSearchTool:
    name = "graph_search"
    description = (
        "Traverse the knowledge graph from a starting entity to find related "
        "nodes and relationships. Best for 'what is related to X?' questions."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "entity": {"type": "string", "description": "Starting node name or ID"},
            "relation": {
                "type": "string",
                "description": (
                    "Edge type to follow, e.g. 'depends_on', 'authored_by'. Omit to follow all."
                ),
            },
            "hops": {
                "type": "integer",
                "default": 2,
                "minimum": 1,
                "maximum": 4,
                "description": "Graph traversal depth",
            },
            "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
        },
        "required": ["entity"],
    }
    scope = "read"
    parallel_safe = True
    tags = {"rag", "graph"}

    def validate(self, raw: dict) -> dict:
        if not raw.get("entity"):
            raise ValueError("entity is required")
        return {
            "entity": str(raw["entity"]),
            "relation": str(raw["relation"]) if raw.get("relation") else None,
            "hops": int(raw.get("hops", 2)),
            "limit": int(raw.get("limit", 10)),
        }

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        deps: RagDeps = ctx.deps
        nodes = await deps.graph_db.query(
            entity=input["entity"],
            relation=input["relation"],
            hops=input["hops"],
            limit=input["limit"],
        )
        if not nodes:
            return ToolResult(
                content=f"No graph results for entity '{input['entity']}'.",
                summary=f"graph_search({input['entity']!r}) → 0 nodes",
            )

        lines = [f"Graph results for '{input['entity']}' (hops={input['hops']}):"]
        citations = []
        for node in nodes:
            rel = f" —[{node['relation']}]→" if node.get("relation") else ""
            lines.append(f"  {rel} {node['id']}: {node.get('label', '')}")
            if node.get("text"):
                lines.append(f"    {node['text']}")
                citations.append(Citation(id=node["id"], source=node["id"], chunk=node["text"]))

        return ToolResult(
            content="\n".join(lines),
            summary=f"graph_search({input['entity']!r}) → {len(nodes)} nodes",
            citations=citations,
        )

    def summarize(self, input: dict) -> str:
        return f"graph_search({input.get('entity', '?')!r})"


# ── WebSearchTool ─────────────────────────────────────────────────────────────
#
# Live web search. Use when knowledge base results are stale or absent.
# In production wire up Tavily, Brave, or SerpAPI as the deps.web_client.


class WebSearchTool:
    name = "web_search"
    description = (
        "Search the live web for up-to-date information. "
        "Use when the knowledge base has no answer or the topic is recent."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "top_k": {"type": "integer", "default": 5, "minimum": 1, "maximum": 10},
        },
        "required": ["query"],
    }
    scope = "read"
    parallel_safe = True
    tags = {"rag", "web"}

    def validate(self, raw: dict) -> dict:
        if not raw.get("query"):
            raise ValueError("query is required")
        return {"query": str(raw["query"]), "top_k": int(raw.get("top_k", 5))}

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        deps: RagDeps = ctx.deps
        results = await deps.web_client.search(input["query"], top_k=input["top_k"])
        citations = [
            Citation(
                id=r.get("url", str(i)),
                source=r.get("url", ""),
                label=r.get("title"),
                chunk=r.get("snippet"),
                score=r.get("score"),
            )
            for i, r in enumerate(results)
        ]
        content = (
            "\n\n".join(
                f"[{r.get('title', r.get('url', i))}]\n{r.get('url', '')}\n{r.get('snippet', '')}"
                for i, r in enumerate(results)
            )
            or "No web results found."
        )
        return ToolResult(
            content=content,
            summary=f"web_search({input['query'][:40]!r}) → {len(results)} results",
            citations=citations,
        )

    def summarize(self, input: dict) -> str:
        return f"web_search({input.get('query', '?')[:40]!r})"


# ── In-memory stubs (replace with real clients in production) ─────────────────


class StubVectorStore:
    _docs = [
        {
            "id": "doc-1",
            "title": "AgentKit overview",
            "text": "AgentKit is an async Python SDK for building agent loops.",
            "score": 0.92,
            "source": "docs/overview.md",
        },
        {
            "id": "doc-2",
            "title": "Tool protocol",
            "text": "Tools are duck-typed protocols with name, description, input_schema, scope.",
            "score": 0.85,
            "source": "docs/tools.md",
        },
    ]

    async def search(self, query: str, top_k: int = 5) -> list[dict]:
        return self._docs[:top_k]


class StubKeywordIndex:
    _docs = [
        {
            "id": "doc-2",
            "text": "Tools are duck-typed protocols with name, description, input_schema, scope.",
            "score": 12.4,
            "source": "docs/tools.md",
        },
        {
            "id": "doc-3",
            "text": "Sessions hold provider_view and full_history separately.",
            "score": 9.1,
            "source": "docs/sessions.md",
        },
    ]

    async def search(self, query: str, top_k: int = 5, filter: dict | None = None) -> list[dict]:
        return self._docs[:top_k]


class StubGraphDB:
    async def query(self, entity: str, relation: str | None, hops: int, limit: int) -> list[dict]:
        return [
            {
                "id": "Session",
                "relation": "uses",
                "label": "Session",
                "text": "Manages per-conversation state.",
            },
            {
                "id": "ToolRegistry",
                "relation": "contains",
                "label": "ToolRegistry",
                "text": "Holds available tools.",
            },
        ][:limit]


class StubWebClient:
    async def search(self, query: str, top_k: int = 5) -> list[dict]:
        return [
            {
                "title": "AgentKit on PyPI",
                "url": "https://pypi.org/project/agent-kit/",
                "snippet": "Async agent loop SDK for Python.",
            },
        ][:top_k]


# ── Wire everything together ──────────────────────────────────────────────────


def build_rag_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(HybridSearchTool())
    registry.register(KeywordSearchTool())
    registry.register(GraphSearchTool())
    registry.register(WebSearchTool())
    return registry


async def main() -> None:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("Set ANTHROPIC_API_KEY to run this example.")
        return

    deps = RagDeps(
        vector_store=StubVectorStore(),
        keyword_index=StubKeywordIndex(),
        graph_db=StubGraphDB(),
        web_client=StubWebClient(),
    )

    agent = Agent(
        model="claude-sonnet-4-6",
        provider=AnthropicProvider(AnthropicProviderOptions(api_key=api_key)),
        system_prompt_config=SystemPromptConfig(
            replace_defaults=True,
            append=(
                "You are a RAG assistant. Use the search tools to find relevant "
                "information before answering. Prefer hybrid_search for general "
                "questions, keyword_search for exact terms, graph_search for "
                "relationship questions, and web_search for current events."
            ),
        ),
        tools=build_rag_registry(),
        deps=deps,
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
    )

    session = await agent.session()
    async for event in session.run("What is AgentKit and how are tools structured?"):
        if event.type == "result":
            print("Answer:", event.final_text)


if __name__ == "__main__":
    asyncio.run(main())
