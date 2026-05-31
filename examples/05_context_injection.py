"""Context injection — RAG per-turn and dynamic context shaping.

Run:
    OPENAI_API_KEY=sk-... python examples/05_context_injection.py

Demonstrates:
  1. Simple per-turn injector — append fresh context before each call
  2. Sliding-window with prune_tagged — prevent unbounded context growth
  3. deps-driven injector — injector reads from ctx.deps
  4. extra_system injection — add ephemeral system blocks per turn
  5. Multi-source injector — compose multiple injectors
  6. Query-aware injection — re-run retrieval based on the actual prompt
"""

from __future__ import annotations

import asyncio
import os

from agent_kit import Agent, RunOptions
from agent_kit.config import FeatureFlags, SystemPromptConfig
from agent_kit.context_hooks import TurnContext, prune_tagged
from agent_kit.sessions import InMemorySessionStore
from agent_kit.tools.registry import empty_tools
from agent_kit.types import Message, SystemBlock, TextBlock

API_KEY = os.environ.get("OPENAI_API_KEY", "")
MODEL = "gpt-5-nano-2025-08-07"

BASE = dict(
    model=MODEL,
    openai_api_key=API_KEY,
    session_store=InMemorySessionStore(),
    features=FeatureFlags(skills=False, subagents=False, mcp=False),
)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _last_user_text(provider_view: list[Message]) -> str:
    """Return the most recent user-typed text (skip env/injected blocks)."""
    for msg in reversed(provider_view):
        if msg.role != "user":
            continue
        for blk in msg.content:
            if (
                isinstance(blk, TextBlock)
                and not blk.text.startswith("<env>")
                and not blk.text.startswith("[[")   # skip injection tags
                and not blk.text.startswith("[db")
            ):
                return blk.text
    return ""


# ── 1. Simple injector ────────────────────────────────────────────────────────
#
# Appends a fixed context block before every provider call.
# Good for: always-on factual context (company info, date, user details).

class UserContextInjector:
    """Inject the current user's profile into every turn."""

    def __init__(self, user_profile: dict) -> None:
        self._profile = user_profile

    async def before_turn(self, ctx: TurnContext) -> None:
        profile = self._profile
        block = (
            f"Current user: {profile.get('name', 'Unknown')}, "
            f"Plan: {profile.get('plan', 'free')}, "
            f"Language: {profile.get('language', 'en')}"
        )
        ctx.provider_view.append(
            Message(role="user", content=[TextBlock(text=f"[user-ctx] {block}")])
        )


# ── 2. Sliding-window injector with prune_tagged ──────────────────────────────
#
# On every turn, remove the previous injection before adding a new one.
# This keeps context fresh without duplicating content across turns.

TAG_KB = "[[kb-ctx]]"


class KnowledgeBaseInjector:
    """Retrieve from a KB and inject, pruning the previous turn's content."""

    def __init__(self, kb: dict[str, str]) -> None:
        self._kb = kb

    async def before_turn(self, ctx: TurnContext) -> None:
        # Step 1: remove previous KB injection
        prune_tagged(ctx.provider_view, TAG_KB)

        # Step 2: find what the user just asked
        query = _last_user_text(ctx.provider_view).lower()

        # Step 3: retrieve relevant entries
        hits = [v for k, v in self._kb.items() if k in query]
        if not hits:
            return

        content = "\n".join(f"• {h}" for h in hits[:3])
        ctx.provider_view.append(
            Message(
                role="user",
                content=[TextBlock(text=f"{TAG_KB}\nRelevant KB entries:\n{content}")],
            )
        )


# ── 3. deps-driven injector ───────────────────────────────────────────────────
#
# ctx.deps carries whatever was passed as Agent(deps=...) or RunOptions(deps=...).
# The injector reads from it without needing __init__ parameters.

TAG_SCHEMA = "[db-schema]"


class SchemaInjector:
    """Inject the live DB schema from ctx.deps into every SQL turn."""

    async def before_turn(self, ctx: TurnContext) -> None:
        prune_tagged(ctx.provider_view, TAG_SCHEMA)

        # deps is expected to be a dict with a "schema" key
        deps = ctx.deps
        schema = deps.get("schema") if isinstance(deps, dict) else None
        if not schema:
            return

        ctx.provider_view.append(
            Message(
                role="user",
                content=[TextBlock(text=f"{TAG_SCHEMA}\nCurrent DB schema:\n{schema}")],
            )
        )


# ── 4. extra_system injection ─────────────────────────────────────────────────
#
# Append SystemBlock objects to ctx.extra_system — they are merged into the
# ProviderRequest.system for that turn ONLY and do not persist in provider_view.
# Good for: turn-level constraints, real-time config flags, dynamic instructions.

class TurnConstraintInjector:
    """Add per-turn constraints as ephemeral system blocks."""

    def __init__(self, get_constraints) -> None:
        # get_constraints(turn_index) -> list[str]
        self._get = get_constraints

    async def before_turn(self, ctx: TurnContext) -> None:
        constraints = self._get(ctx.turn_index)
        if constraints:
            text = "Turn constraints:\n" + "\n".join(f"- {c}" for c in constraints)
            ctx.extra_system.append(SystemBlock(text=text, cacheable=False))


# ── 5. Multi-source injector composition ─────────────────────────────────────
#
# Pass multiple injectors to Agent. They all fire in order on each turn.

def make_rag_agent(kb: dict[str, str], user_profile: dict) -> Agent:
    return Agent(
        **BASE,
        tools=empty_tools(),
        context_injectors=[
            UserContextInjector(user_profile),
            KnowledgeBaseInjector(kb),
        ],
        system_prompt_config=SystemPromptConfig(
            replace_defaults=True,
            append=(
                "You are a customer support assistant. "
                "Use the provided context to answer accurately. "
                "Greet the user by name if known."
            ),
        ),
    )


# ── 6. Query-aware injector ───────────────────────────────────────────────────
#
# More realistic: run vector search based on what the user actually said.
# The injector has async execute — works with real async vector stores.

TAG_DOCS = "[[docs]]"


class VectorSearchInjector:
    """Async injector using ctx.deps as a vector store."""

    async def before_turn(self, ctx: TurnContext) -> None:
        prune_tagged(ctx.provider_view, TAG_DOCS)

        vector_store = ctx.deps  # passed as Agent(deps=...) or RunOptions(deps=...)
        if vector_store is None:
            return

        query = _last_user_text(ctx.provider_view)
        if not query:
            return

        # Call the store — supports both sync and async search
        try:
            if asyncio.iscoroutinefunction(vector_store.search):
                docs = await vector_store.search(query, top_k=3)
            else:
                docs = vector_store.search(query, top_k=3)
        except Exception:
            return

        if docs:
            ctx.provider_view.append(
                Message(
                    role="user",
                    content=[TextBlock(text=f"{TAG_DOCS}\nRetrieved:\n{docs}")],
                )
            )


# Simple in-memory vector store for the demo
class SimpleVectorStore:
    def __init__(self, docs: dict[str, str]) -> None:
        self._docs = docs

    def search(self, query: str, top_k: int = 3) -> str:
        hits = [
            v for k, v in self._docs.items()
            if any(word in k for word in query.lower().split())
        ]
        return "\n".join(hits[:top_k])


# ── Live demos ─────────────────────────────────────────────────────────────────


async def demo_sliding_window() -> None:
    print("\n── 2. Sliding-window KB injector ──")
    kb = {
        "return": "Returns accepted within 30 days with receipt.",
        "shipping": "Free shipping on orders over $50.",
        "hours": "Support hours: Mon–Fri 9am–6pm EST.",
        "refund": "Refunds processed within 5–7 business days.",
    }
    agent = make_rag_agent(kb, user_profile={"name": "Alice", "plan": "pro"})
    session = await agent.session()

    for question in [
        "What is your return policy?",
        "When can I expect my refund?",
    ]:
        result = None
        async for event in session.run(question):
            if event.type == "result":
                result = event
        print(f"  Q: {question}")
        print(f"  A: {result.final_text[:150]}\n")


async def demo_schema_injector() -> None:
    print("\n── 3. DB schema injector via ctx.deps ──")
    deps_v1 = {"schema": "CREATE TABLE users (id INT, name TEXT, email TEXT);"}
    deps_v2 = {"schema": "CREATE TABLE orders (id INT, user_id INT, total FLOAT, status TEXT);"}

    agent = Agent(
        **BASE,
        tools=empty_tools(),
        context_injectors=[SchemaInjector()],
        deps=deps_v1,
        system_prompt_config=SystemPromptConfig(
            replace_defaults=True,
            append="You are a SQL assistant. Use the injected schema to answer.",
        ),
    )

    # Session 1 uses the default deps (users table)
    s1 = await agent.session()
    r1 = None
    async for event in s1.run("Count all users."):
        if event.type == "result":
            r1 = event
    print(f"  With users schema: {r1.final_text[:100]}")

    # Session 2 uses RunOptions(deps=...) to override (orders table)
    s2 = await agent.session()
    r2 = None
    async for event in s2.run("Count all pending orders.", RunOptions(deps=deps_v2)):
        if event.type == "result":
            r2 = event
    print(f"  With orders schema: {r2.final_text[:100]}")


async def demo_vector_search() -> None:
    print("\n── 4. Async vector-search injector ──")
    store = SimpleVectorStore({
        "weather forecast": "Tomorrow will be 24°C with sunny skies in most regions.",
        "weekend weather": "Expect scattered showers this weekend.",
        "travel advice": "Check visa requirements before booking international flights.",
        "flight booking": "Book flights 6–8 weeks in advance for the best prices.",
    })

    agent = Agent(
        **BASE,
        tools=empty_tools(),
        context_injectors=[VectorSearchInjector()],
        deps=store,
        system_prompt_config=SystemPromptConfig(
            replace_defaults=True,
            append="You are a helpful assistant. Use retrieved context when available.",
        ),
    )
    session = await agent.session()
    result = None
    async for event in session.run("What's the weather like this weekend?"):
        if event.type == "result":
            result = event
    print(f"  Answer: {result.final_text[:200]}")


async def main() -> None:
    if not API_KEY:
        print("Set OPENAI_API_KEY to run this example.")
        # Show that injectors construct correctly
        kb = {"test": "Test entry."}
        agents = [
            ("user ctx", Agent(**BASE, tools=empty_tools(),
                               context_injectors=[UserContextInjector({"name": "T"})])),
            ("kb injector", Agent(**BASE, tools=empty_tools(),
                                  context_injectors=[KnowledgeBaseInjector(kb)])),
            ("schema injector", Agent(**BASE, tools=empty_tools(),
                                      context_injectors=[SchemaInjector()],
                                      deps={"schema": "CREATE TABLE t (id INT);"})),
        ]
        for label, a in agents:
            print(f"  {label}: {len(a.context_injectors)} injector(s)")
        return

    await demo_sliding_window()
    await demo_schema_injector()
    await demo_vector_search()


if __name__ == "__main__":
    asyncio.run(main())
