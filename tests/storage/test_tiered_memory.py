"""Tests for Feature G — TieredMemoryStore hierarchical memory tiers.

Tests cover:
- upsert routing by metadata["tier"]
- search merging / global ranking / dedup
- tier stamp on results
- namespace + metadata_filter passthrough
- protocol conformance (duck-typed resolve_memory_store)
- tier-aware MemoryContextBuilder (group_by_tier=True)
- regression guard: default flat output unchanged
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Helpers: test provider (lazy-import safe)
# ---------------------------------------------------------------------------


class _RecordingProvider:
    id = "fake"

    def __init__(self) -> None:
        self.calls: list = []

    def context_window(self, model: str) -> int:
        return 128_000

    async def stream(self, req):
        from linch.types import TextBlock, Usage

        self.calls.append(
            {
                "messages": [
                    {
                        "role": message.role,
                        "content": [
                            block.text if isinstance(block, TextBlock) else str(block)
                            for block in message.content
                        ],
                    }
                    for message in req.messages
                ],
            }
        )
        yield {"type": "message_start", "model": req.model}
        yield {"type": "text_delta", "text": "done"}
        yield {
            "type": "message_end",
            "stop_reason": "end_turn",
            "usage": Usage(),
            "provider_metadata": None,
        }


# ---------------------------------------------------------------------------
# Unit: upsert routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tiered_upsert_routes_by_tier_metadata() -> None:
    """Items tagged metadata['tier'] land only in the matching sub-store."""
    from linch.memory import InMemoryKeywordMemoryStore, MemoryItem
    from linch.memory.tiered import TieredMemoryStore

    working = InMemoryKeywordMemoryStore()
    episodic = InMemoryKeywordMemoryStore()
    semantic = InMemoryKeywordMemoryStore()
    store = TieredMemoryStore(working=working, episodic=episodic, semantic=semantic)

    await store.upsert(
        [
            MemoryItem(id="w1", content="working fact", metadata={"tier": "working"}),
            MemoryItem(id="e1", content="episodic event", metadata={"tier": "episodic"}),
            MemoryItem(id="s1", content="semantic knowledge", metadata={"tier": "semantic"}),
        ]
    )

    assert [item.id for item in working.list()] == ["w1"]
    assert [item.id for item in episodic.list()] == ["e1"]
    assert [item.id for item in semantic.list()] == ["s1"]


@pytest.mark.asyncio
async def test_tiered_upsert_defaults_untagged_to_working() -> None:
    """Items without a 'tier' key in metadata default to the working tier."""
    from linch.memory import InMemoryKeywordMemoryStore, MemoryItem
    from linch.memory.tiered import TieredMemoryStore

    working = InMemoryKeywordMemoryStore()
    episodic = InMemoryKeywordMemoryStore()
    semantic = InMemoryKeywordMemoryStore()
    store = TieredMemoryStore(working=working, episodic=episodic, semantic=semantic)

    await store.upsert([MemoryItem(id="u1", content="untagged content")])

    assert [item.id for item in working.list()] == ["u1"]
    assert episodic.list() == []
    assert semantic.list() == []


@pytest.mark.asyncio
async def test_tiered_upsert_unknown_tier_defaults_to_working() -> None:
    """Items with an unrecognized tier value default to working."""
    from linch.memory import InMemoryKeywordMemoryStore, MemoryItem
    from linch.memory.tiered import TieredMemoryStore

    working = InMemoryKeywordMemoryStore()
    episodic = InMemoryKeywordMemoryStore()
    semantic = InMemoryKeywordMemoryStore()
    store = TieredMemoryStore(working=working, episodic=episodic, semantic=semantic)

    await store.upsert([MemoryItem(id="x1", content="unknown tier", metadata={"tier": "longterm"})])

    assert [item.id for item in working.list()] == ["x1"]
    assert episodic.list() == []
    assert semantic.list() == []


@pytest.mark.asyncio
async def test_tiered_upsert_unhashable_tier_defaults_to_working() -> None:
    """Items with non-string tier metadata default to working instead of raising."""
    from linch.memory import InMemoryKeywordMemoryStore, MemoryItem
    from linch.memory.tiered import TieredMemoryStore

    working = InMemoryKeywordMemoryStore()
    store = TieredMemoryStore(
        working=working,
        episodic=InMemoryKeywordMemoryStore(),
        semantic=InMemoryKeywordMemoryStore(),
    )

    await store.upsert([MemoryItem(id="x1", content="list tier", metadata={"tier": ["bad"]})])

    assert [item.id for item in working.list()] == ["x1"]


# ---------------------------------------------------------------------------
# Unit: search merging and ranking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tiered_search_merges_and_ranks_across_tiers() -> None:
    """Search fans out across all tiers, merging by global (score, id) desc."""
    from linch.memory import InMemoryKeywordMemoryStore, MemoryItem
    from linch.memory.tiered import TieredMemoryStore

    working = InMemoryKeywordMemoryStore()
    episodic = InMemoryKeywordMemoryStore()
    semantic = InMemoryKeywordMemoryStore()
    store = TieredMemoryStore(working=working, episodic=episodic, semantic=semantic)

    # w1 matches 2/2 query terms ("parallel search") → score 1.0
    # e1 matches 1/2 ("parallel") → score 0.5
    # s1 matches 1/2 ("search") → score 0.5
    # At score 0.5 tie: sort key is (0.5, id) reversed → "s1" > "e1"
    await store.upsert(
        [
            MemoryItem(id="w1", content="parallel search", metadata={"tier": "working"}),
            MemoryItem(id="e1", content="parallel event", metadata={"tier": "episodic"}),
            MemoryItem(id="s1", content="search knowledge", metadata={"tier": "semantic"}),
        ]
    )

    hits = await store.search("parallel search", limit=10)
    ids = [hit.item.id for hit in hits]
    assert ids == ["w1", "s1", "e1"]


@pytest.mark.asyncio
async def test_tiered_search_respects_global_limit() -> None:
    """Global limit slices the merged result set after all tiers contribute."""
    from linch.memory import InMemoryKeywordMemoryStore, MemoryItem
    from linch.memory.tiered import TieredMemoryStore

    working = InMemoryKeywordMemoryStore()
    episodic = InMemoryKeywordMemoryStore()
    semantic = InMemoryKeywordMemoryStore()
    store = TieredMemoryStore(working=working, episodic=episodic, semantic=semantic)

    await store.upsert(
        [
            MemoryItem(id="w1", content="alpha beta", metadata={"tier": "working"}),
            MemoryItem(id="e1", content="alpha gamma", metadata={"tier": "episodic"}),
            MemoryItem(id="s1", content="alpha delta", metadata={"tier": "semantic"}),
        ]
    )

    hits = await store.search("alpha", limit=2)
    assert len(hits) == 2


@pytest.mark.asyncio
async def test_tiered_limits_hard_cap_can_exclude_global_top_n() -> None:
    """A small tier_limit is a HARD per-tier cap applied before the global merge.

    Pins the documented semantics: with tier_limits set small for one tier, a
    globally higher-scoring item in that tier is dropped pre-merge; the default
    (no tier_limits) returns the global top-N including that item.
    """
    from linch.memory import InMemoryKeywordMemoryStore, MemoryItem
    from linch.memory.tiered import TieredMemoryStore

    def _stores() -> dict[str, InMemoryKeywordMemoryStore]:
        return {tier: InMemoryKeywordMemoryStore() for tier in ("working", "episodic", "semantic")}

    # The working tier holds the two best matches (score 1.0 and 0.66). With a
    # working-tier cap of 1, only the single best survives the tier query, so the
    # second-best (still globally higher than other tiers' hits) is dropped.
    async def _seed(store: TieredMemoryStore) -> None:
        await store.upsert(
            [
                MemoryItem(id="w_best", content="alpha beta gamma", metadata={"tier": "working"}),
                MemoryItem(id="w_second", content="alpha beta", metadata={"tier": "working"}),
                MemoryItem(id="e1", content="alpha", metadata={"tier": "episodic"}),
            ]
        )

    # Hard cap path: working tier limited to 1 → w_second excluded pre-merge.
    capped = TieredMemoryStore(**_stores(), tier_limits={"working": 1})
    await _seed(capped)
    capped_ids = [hit.item.id for hit in await capped.search("alpha beta gamma", limit=10)]
    assert "w_best" in capped_ids
    assert "w_second" not in capped_ids, "hard per-tier cap should drop the second working hit"

    # Default path: no tier_limits → global top-N includes w_second.
    default = TieredMemoryStore(**_stores())
    await _seed(default)
    default_ids = [hit.item.id for hit in await default.search("alpha beta gamma", limit=10)]
    assert {"w_best", "w_second"} <= set(default_ids)


@pytest.mark.asyncio
async def test_tiered_search_limit_zero_returns_empty() -> None:
    """limit=0 returns empty list (matches InMemoryKeywordMemoryStore behaviour)."""
    from linch.memory import InMemoryKeywordMemoryStore, MemoryItem
    from linch.memory.tiered import TieredMemoryStore

    working = InMemoryKeywordMemoryStore()
    store = TieredMemoryStore(
        working=working,
        episodic=InMemoryKeywordMemoryStore(),
        semantic=InMemoryKeywordMemoryStore(),
    )
    await store.upsert([MemoryItem(id="w1", content="alpha", metadata={"tier": "working"})])

    hits = await store.search("alpha", limit=0)
    assert hits == []


@pytest.mark.asyncio
async def test_tiered_search_stamps_source_tier() -> None:
    """Each result carries its source tier in result.metadata['tier']."""
    from linch.memory import InMemoryKeywordMemoryStore, MemoryItem
    from linch.memory.tiered import TieredMemoryStore

    working = InMemoryKeywordMemoryStore()
    episodic = InMemoryKeywordMemoryStore()
    semantic = InMemoryKeywordMemoryStore()
    store = TieredMemoryStore(working=working, episodic=episodic, semantic=semantic)

    await store.upsert(
        [
            MemoryItem(id="w1", content="working fact", metadata={"tier": "working"}),
            MemoryItem(id="s1", content="semantic knowledge", metadata={"tier": "semantic"}),
        ]
    )

    hits = await store.search("fact knowledge", limit=10)
    tier_by_id = {hit.item.id: hit.metadata.get("tier") for hit in hits}
    assert tier_by_id["w1"] == "working"
    assert tier_by_id["s1"] == "semantic"


@pytest.mark.asyncio
async def test_memory_search_tool_preserves_tier_metadata() -> None:
    """SearchMemory exposes tier counts and citation tier metadata for reports."""
    from linch.memory import InMemoryKeywordMemoryStore, MemoryItem, MemorySearchTool
    from linch.memory.tiered import TieredMemoryStore
    from linch.tools import ToolContext

    store = TieredMemoryStore(
        working=InMemoryKeywordMemoryStore(),
        episodic=InMemoryKeywordMemoryStore(),
        semantic=InMemoryKeywordMemoryStore(),
    )
    await store.upsert(
        [
            MemoryItem(
                id="s1",
                content="pto rollover policy",
                metadata={"tier": "semantic"},
                namespace="tenant-a",
            )
        ]
    )

    tool = MemorySearchTool(store)
    result = await tool.execute(
        {"query": "pto rollover", "limit": 5, "namespace": "tenant-a"},
        ToolContext(cwd=".", session_id="s", run_id="r", session_store=None),
    )

    assert result.metadata["result_ids"] == ["s1"]
    assert result.metadata["tier_counts"] == {"semantic": 1}
    assert result.citations[0].metadata["tier"] == "semantic"


@pytest.mark.asyncio
async def test_tiered_search_tier_stamp_does_not_mutate_original() -> None:
    """Stamping 'tier' on result.metadata does not mutate the sub-store's result dict."""
    from linch.memory import InMemoryKeywordMemoryStore, MemoryItem
    from linch.memory.tiered import TieredMemoryStore

    working = InMemoryKeywordMemoryStore()
    store = TieredMemoryStore(
        working=working,
        episodic=InMemoryKeywordMemoryStore(),
        semantic=InMemoryKeywordMemoryStore(),
    )
    await store.upsert([MemoryItem(id="w1", content="fact content", metadata={"tier": "working"})])

    # First search via the sub-store directly (to get the original metadata dict)
    original_hits = await working.search("fact content")
    original_meta = original_hits[0].metadata

    # Search via tiered store
    tiered_hits = await store.search("fact content", limit=5)
    assert tiered_hits[0].metadata.get("tier") == "working"

    # Original sub-store result's metadata should be unchanged
    assert "tier" not in original_meta


@pytest.mark.asyncio
async def test_tiered_search_namespace_and_metadata_filter_passthrough() -> None:
    """namespace and metadata_filter are forwarded to each sub-store."""
    from linch.memory import InMemoryKeywordMemoryStore, MemoryItem
    from linch.memory.tiered import TieredMemoryStore

    working = InMemoryKeywordMemoryStore()
    episodic = InMemoryKeywordMemoryStore()
    semantic = InMemoryKeywordMemoryStore()
    store = TieredMemoryStore(working=working, episodic=episodic, semantic=semantic)

    await store.upsert(
        [
            MemoryItem(
                id="w1",
                content="alpha beta",
                metadata={"tier": "working", "kind": "fact"},
                namespace="ns1",
            ),
            MemoryItem(
                id="w2",
                content="alpha beta",
                metadata={"tier": "working", "kind": "event"},
                namespace="ns1",
            ),
            MemoryItem(
                id="e1",
                content="alpha beta",
                metadata={"tier": "episodic"},
                namespace="ns2",
            ),
        ]
    )

    # namespace filter: only ns1 results
    hits_ns1 = await store.search("alpha", namespace="ns1", limit=10)
    hit_ids = {hit.item.id for hit in hits_ns1}
    assert "e1" not in hit_ids
    assert hit_ids <= {"w1", "w2"}

    # metadata_filter: only kind=fact
    hits_fact = await store.search("alpha", metadata_filter={"kind": "fact"}, limit=10)
    assert [hit.item.id for hit in hits_fact] == ["w1"]


@pytest.mark.asyncio
async def test_tiered_search_deduplicates_by_id_keeps_higher_score() -> None:
    """If the same item id appears in multiple tiers, keep only the higher-scored result."""
    from linch.memory import InMemoryKeywordMemoryStore, MemoryItem
    from linch.memory.tiered import TieredMemoryStore

    working = InMemoryKeywordMemoryStore()
    episodic = InMemoryKeywordMemoryStore()
    semantic = InMemoryKeywordMemoryStore()
    store = TieredMemoryStore(working=working, episodic=episodic, semantic=semantic)

    # Manually upsert same id into two sub-stores to simulate the edge case.
    # working: "alpha beta gamma" matches 3/3 query terms → score 1.0
    # episodic: "alpha" matches 1/3 query terms → score 0.33
    await working.upsert(
        [MemoryItem(id="dup", content="alpha beta gamma", metadata={"tier": "working"})]
    )
    await episodic.upsert([MemoryItem(id="dup", content="alpha", metadata={"tier": "episodic"})])

    hits = await store.search("alpha beta gamma", limit=10)
    dup_hits = [h for h in hits if h.item.id == "dup"]
    assert len(dup_hits) == 1
    # Working tier has the higher score — its result should survive.
    assert dup_hits[0].metadata.get("tier") == "working"


@pytest.mark.asyncio
async def test_tiered_search_does_not_deduplicate_across_namespaces() -> None:
    """The same item id in different namespaces represents distinct memories."""
    from linch.memory import InMemoryKeywordMemoryStore, MemoryItem
    from linch.memory.tiered import TieredMemoryStore

    working = InMemoryKeywordMemoryStore()
    semantic = InMemoryKeywordMemoryStore()
    store = TieredMemoryStore(
        working=working,
        episodic=InMemoryKeywordMemoryStore(),
        semantic=semantic,
    )

    await working.upsert([MemoryItem(id="same", content="alpha beta", namespace="tenant-a")])
    await semantic.upsert([MemoryItem(id="same", content="alpha beta", namespace="tenant-b")])

    hits = await store.search("alpha beta", limit=10)
    keys = {(hit.item.namespace, hit.item.id) for hit in hits}

    assert keys == {("tenant-a", "same"), ("tenant-b", "same")}


@pytest.mark.asyncio
async def test_tiered_search_filters_extra_kwargs_per_store_signature() -> None:
    """Extra search kwargs are only sent to stores that declare support for them."""
    from linch.memory import InMemoryKeywordMemoryStore, MemoryItem, MemorySearchResult
    from linch.memory.tiered import TieredMemoryStore

    class ExtraKwStore:
        def __init__(self) -> None:
            self.include_score_seen = False

        async def upsert(self, items, **kwargs) -> None:
            pass

        async def search(self, query: str, *, limit: int = 5, include_score: bool = False):
            self.include_score_seen = include_score
            return [
                MemorySearchResult(
                    item=MemoryItem(id="extra", content=query),
                    score=1.0,
                )
            ]

    class StrictStore:
        async def upsert(self, items) -> None:
            pass

        async def search(self, query: str, *, limit: int = 5):
            return [
                MemorySearchResult(
                    item=MemoryItem(id="strict", content=query),
                    score=0.5,
                )
            ]

    strict = StrictStore()
    extra = ExtraKwStore()
    store = TieredMemoryStore(
        working=extra,
        episodic=strict,
        semantic=InMemoryKeywordMemoryStore(),
    )

    hits = await store.search("alpha", limit=10, include_score=True)

    assert extra.include_score_seen is True
    assert {hit.item.id for hit in hits} >= {"extra", "strict"}


# ---------------------------------------------------------------------------
# Unit: protocol conformance + exports
# ---------------------------------------------------------------------------


def test_tiered_store_satisfies_memory_protocol() -> None:
    """resolve_memory_store recognizes TieredMemoryStore via duck-typing."""
    from linch.memory import InMemoryKeywordMemoryStore
    from linch.memory.store import resolve_memory_store
    from linch.memory.tiered import TieredMemoryStore

    ts = TieredMemoryStore(
        working=InMemoryKeywordMemoryStore(),
        episodic=InMemoryKeywordMemoryStore(),
        semantic=InMemoryKeywordMemoryStore(),
    )
    assert resolve_memory_store(ts) is ts


def test_tiered_store_importable_from_linch_memory() -> None:
    """TieredMemoryStore is importable from linch.memory."""
    from linch.memory import TieredMemoryStore  # noqa: F401

    assert TieredMemoryStore is not None


def test_tiered_store_importable_from_root() -> None:
    """TieredMemoryStore is importable from linch."""
    from linch import TieredMemoryStore  # noqa: F401

    assert TieredMemoryStore is not None


# ---------------------------------------------------------------------------
# Integration: tier-aware MemoryContextBuilder
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tiered_builder_groups_by_tier() -> None:
    """group_by_tier=True injects tier subheadings and remains ephemeral."""
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.hooks import ContextInjectionHook
    from linch.memory import InMemoryKeywordMemoryStore, MemoryContextBuilder, MemoryItem
    from linch.memory.tiered import TieredMemoryStore
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools

    working = InMemoryKeywordMemoryStore()
    episodic = InMemoryKeywordMemoryStore()
    semantic = InMemoryKeywordMemoryStore()
    tiered = TieredMemoryStore(working=working, episodic=episodic, semantic=semantic)

    await tiered.upsert(
        [
            MemoryItem(id="w1", content="recent task context", metadata={"tier": "working"}),
            MemoryItem(
                id="s1",
                content="long-term semantic knowledge",
                metadata={"tier": "semantic"},
            ),
        ]
    )

    provider = _RecordingProvider()
    agent = Agent(
        model="fake-model",
        provider=provider,
        tools=empty_tools(),
        deps=tiered,
        hooks=[ContextInjectionHook(MemoryContextBuilder(group_by_tier=True))],
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
    )
    session = await agent.session()
    async for _ in session.run("recent task semantic knowledge"):
        pass

    # At least one tier subheading must appear in the injected context.
    texts = [text for msg in provider.calls[0]["messages"] for text in msg["content"]]
    full_text = " ".join(texts)
    assert "working" in full_text.lower(), "Expected 'working' tier subheading in injected context"

    # The injected context must NOT be persisted into provider_view.
    persisted_texts = [
        block.text
        for message in session.provider_view
        for block in message.content
        if hasattr(block, "text")
    ]
    assert all("Retrieved memory" not in t for t in persisted_texts)


@pytest.mark.asyncio
async def test_builder_default_flat_output_unchanged() -> None:
    """Default group_by_tier=False produces the same flat 'Retrieved memory:' output."""
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.hooks import ContextInjectionHook
    from linch.memory import InMemoryKeywordMemoryStore, MemoryContextBuilder, MemoryItem
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools

    store = InMemoryKeywordMemoryStore()
    await store.upsert(
        [MemoryItem(id="m1", content="scheduler runs in parallel", namespace="docs")]
    )

    provider = _RecordingProvider()
    agent = Agent(
        model="fake-model",
        provider=provider,
        tools=empty_tools(),
        deps=store,
        hooks=[ContextInjectionHook(MemoryContextBuilder(namespace="docs"))],
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
    )
    session = await agent.session()
    async for _ in session.run("parallel scheduler"):
        pass

    texts = [text for msg in provider.calls[0]["messages"] for text in msg["content"]]
    assert any("Retrieved memory:" in t for t in texts)
    assert any("scheduler runs in parallel" in t for t in texts)


def test_grouped_memory_context_unknown_tier_renders_under_working() -> None:
    """Unknown tier metadata should not cause grouped context to drop results."""
    from linch.memory import MemoryItem, MemorySearchResult
    from linch.memory.builder import format_memory_context_grouped

    content = format_memory_context_grouped(
        [
            MemorySearchResult(
                item=MemoryItem(id="m1", content="important memory"),
                score=1.0,
                metadata={"tier": "archive"},
            )
        ]
    )

    assert "Retrieved memory (working):" in content
    assert "important memory" in content


# ---------------------------------------------------------------------------
# Postgres keyword-search recency-cap regression (Issue 1)
#
# Requires a live Postgres DB; skipped without one — same guard as
# tests/storage/test_postgres.py.  This test exercises the REAL query path
# (no faked DB).  Locally it SKIPS unless AGENT_KIT_TEST_PG_DSN is set:
#     pip install 'linch[postgres]'
#     AGENT_KIT_TEST_PG_DSN=postgresql://user:pw@localhost/agentkit_test \
#         pytest tests/storage/test_tiered_memory.py -v
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pg_search_returns_old_best_match_beyond_recency_cap() -> None:
    """An old-but-best-matching row is returned even past the former recency cap.

    Regression for Issue 1: the prior implementation pre-fetched only the most
    recently-updated ``max(1000, limit*20)`` rows before Python keyword scoring,
    so an old best match was excluded once the namespace exceeded the cap.  The
    fix scans all candidate rows (like SqliteMemoryStore), so the old match is
    found regardless of recency.

    Skipped unless asyncpg + a live DB (``AGENT_KIT_TEST_PG_DSN``) are
    available — same guard as tests/storage/test_postgres.py.  Exercises the
    REAL query path (no faked DB).
    """
    import os

    pytest.importorskip("asyncpg", reason="asyncpg not installed")
    dsn = os.environ.get("AGENT_KIT_TEST_PG_DSN", "")
    if not dsn:
        pytest.skip("AGENT_KIT_TEST_PG_DSN not set")

    from linch.memory.postgres import PostgresMemoryStore
    from linch.memory.types import MemoryItem

    ns = "pg-recency-cap-test"
    store = PostgresMemoryStore(dsn)
    try:
        # Seed one OLD best-matching row first (its updated_at is earliest),
        # then flood the namespace with > cap (max(1000, limit*20)) filler rows
        # that do NOT match the query, so the best match sorts to the bottom by
        # recency and would be excluded by any recency-capped prefetch.
        await store.upsert(
            [
                MemoryItem(
                    id="old-best",
                    content="quokka xylophone zephyr",
                    namespace=ns,
                )
            ]
        )
        filler = [
            MemoryItem(id=f"filler-{i}", content="unrelated filler text", namespace=ns)
            for i in range(1100)
        ]
        await store.upsert(filler)

        results = await store.search("quokka xylophone zephyr", namespace=ns, limit=5)
        ids = [r.item.id for r in results]
        assert "old-best" in ids, "old best-matching row must survive past the recency cap"
        assert ids[0] == "old-best"
    finally:
        # Clean up the namespace so reruns stay deterministic.
        pool = await store._ensure()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM memories WHERE namespace = $1", ns)
        await store.close()
