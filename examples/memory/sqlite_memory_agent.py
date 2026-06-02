"""Persistent memory with SqliteMemoryStore.

Run:
    python3 examples/memory/sqlite_memory_agent.py

Creates a SQLite database at /tmp/agentkit_demo_memory.db, exercises the
store, then cleans up.  No API key needed for the local demo.

Demonstrates:
  1. SqliteMemoryStore — persists to a real file; survives process restarts.
  2. Round-trip: write in one "session", re-open and read in another.
  3. Upsert (update) an existing memory item by id.
  4. Side-by-side with InMemoryKeywordMemoryStore — shows the difference.
  5. MemoryContextBuilder + MemorySearchTool wired to the SQLite backend.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from linch.memory import (
    InMemoryKeywordMemoryStore,
    MemoryContextBuilder,
    MemoryItem,
    MemorySearchTool,
)
from linch.memory.sqlite import SqliteMemoryStore
from linch.tools import ToolContext

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = Path("/tmp/agentkit_demo_memory.db")
NS = "demo"


def load_project_env() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


SEED_ITEMS = [
    MemoryItem(
        id="m1", content="SqliteMemoryStore persists to a real database file.", namespace=NS
    ),
    MemoryItem(
        id="m2", content="InMemoryKeywordMemoryStore resets when the process exits.", namespace=NS
    ),
    MemoryItem(
        id="m3", content="Both stores implement the same MemoryStore protocol.", namespace=NS
    ),
    MemoryItem(
        id="m4",
        content="MemoryContextBuilder injects recalled items as per-turn context.",
        namespace=NS,
    ),
]


async def demo_persist_round_trip() -> None:
    print("── 1. Write then re-open ─────────────────────────────────────────")
    DB_PATH.unlink(missing_ok=True)

    # "First process" — seed the store
    store = SqliteMemoryStore(DB_PATH)
    await store.upsert(SEED_ITEMS)
    store.close()
    print(f"  Wrote {len(SEED_ITEMS)} items to {DB_PATH}")

    # "Second process" — open fresh and search
    store2 = SqliteMemoryStore(DB_PATH)
    results = await store2.search("persist database protocol", namespace=NS, limit=3)
    print(f"  Re-opened store → {len(results)} hits:")
    for r in results:
        print(f"    [{r.item.id}] score={r.score:.2f}: {r.item.content}")
    store2.close()


async def demo_upsert_update() -> None:
    print("\n── 2. Upsert (update an existing item by id) ─────────────────────")
    store = SqliteMemoryStore(DB_PATH)
    await store.upsert(
        [MemoryItem(id="m1", content="SqliteMemoryStore has been updated in place.", namespace=NS)]
    )
    results = await store.search("updated place", namespace=NS, limit=2)
    print(f"  After update: {results[0].item.content!r}")
    store.close()


async def demo_in_memory_vs_sqlite() -> None:
    print("\n── 3. InMemoryKeywordMemoryStore — fresh instance has nothing ─────")
    mem = InMemoryKeywordMemoryStore()
    await mem.upsert(SEED_ITEMS)
    res = await mem.search("protocol persist database", namespace=NS, limit=3)
    print(f"  Existing InMemory instance: {len(res)} results")

    fresh = InMemoryKeywordMemoryStore()
    empty = await fresh.search("protocol", namespace=NS, limit=3)
    print(f"  Fresh InMemory instance:    {len(empty)} results (expected 0)")


async def demo_context_builder_and_tool() -> None:
    print("\n── 4. MemoryContextBuilder + MemorySearchTool ────────────────────")
    store = SqliteMemoryStore(DB_PATH)

    _ = MemoryContextBuilder(store, namespace=NS, max_tokens=150)
    hits = await store.search("context builder inject", namespace=NS, limit=5)
    print(f"  Builder source hits for query: {len(hits)}")

    search_tool = MemorySearchTool(store, namespace=NS)
    ctx = ToolContext(cwd=str(ROOT), session_id="sqlite-demo", run_id="local", session_store=None)
    result = await search_tool.execute(
        {"query": "protocol persist context inject", "limit": 3, "namespace": NS}, ctx
    )
    print(f"  SearchTool summary: {result.summary}")
    for cite in result.citations:
        print(f"    {cite.id} score={cite.score:.2f}: {cite.label}")
    store.close()


async def maybe_live_agent() -> None:
    load_project_env()
    if not os.environ.get("OPENAI_API_KEY"):
        print("\nOPENAI_API_KEY not set; skipping live agent call.")
        return

    from linch import Agent
    from linch.sessions import InMemorySessionStore
    from linch.tools import ToolRegistry

    store = SqliteMemoryStore(DB_PATH)
    registry = ToolRegistry()
    registry.add(MemorySearchTool(namespace=NS))

    agent = Agent(
        model="gpt-5-nano-2025-08-07",
        openai_api_key=os.environ.get("OPENAI_API_KEY"),
        tools=registry,
        deps=store,
        context_builder=MemoryContextBuilder(namespace=NS, max_tokens=300),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        system_prompt="Use memory context and search tools when relevant.",
    )
    session = await agent.session()
    async for event in session.run("What do you know about SqliteMemoryStore?"):
        if event.type == "result":
            print(f"\nLive answer: {event.final_text}")
    store.close()


async def main() -> None:
    await demo_persist_round_trip()
    await demo_upsert_update()
    await demo_in_memory_vs_sqlite()
    await demo_context_builder_and_tool()
    await maybe_live_agent()
    DB_PATH.unlink(missing_ok=True)
    print("\nCleaned up demo database.")


if __name__ == "__main__":
    asyncio.run(main())
