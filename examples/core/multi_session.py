"""Multi-session patterns — one Agent, many users / conversations.

Run:
    OPENAI_API_KEY=sk-... python examples/06_multi_session.py

Demonstrates:
  1. Basic multi-user: one Agent, one session per user
  2. Persistent sessions: resume a conversation after a restart
  3. Concurrent sessions: run multiple users in parallel
  4. Per-session deps override: tenant-scoped DB connections
  5. Session metadata: store user info, track usage
  6. Web-server-style handler: request → session → events → response

Key principle:
  Agent  = long-lived singleton (create once, share across requests)
  Session = one conversation thread per user / chat window

The Agent holds: model, tools, permissions, system prompt, provider config.
The Session holds: message history, active run state.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from linch import Agent, RunOptions
from linch.config import FeatureFlags, SystemPromptConfig
from linch.sessions import InMemorySessionStore, SqliteSessionStore
from linch.tools.base import ToolContext, ToolResult
from linch.tools.registry import empty_tools

API_KEY = os.environ.get("OPENAI_API_KEY", "")
MODEL = "gpt-5-nano-2025-08-07"


# ── 1. Basic multi-user ───────────────────────────────────────────────────────
#
# Create one Agent at startup. For each user, call agent.session().
# Sessions are independent — Alice's history never bleeds into Bob's.


async def demo_multi_user() -> None:
    print("\n── 1. Multi-user — independent sessions ──")

    agent = Agent(
        model=MODEL,
        openai_api_key=API_KEY,
        session_store=InMemorySessionStore(),
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        system_prompt_config=SystemPromptConfig(
            replace_defaults=True,
            append="You are a helpful assistant. Always remember the user's name if told.",
        ),
        tools=empty_tools(),
    )

    # Simulate two users chatting independently
    alice_session = await agent.session(meta={"user": "alice"})
    bob_session = await agent.session(meta={"user": "bob"})

    # Alice introduces herself
    async for event in alice_session.run("My name is Alice. Hello!"):
        if event.type == "result":
            print(f"  Alice→ {event.final_text[:80]}")

    # Bob introduces himself
    async for event in bob_session.run("My name is Bob. Hi there!"):
        if event.type == "result":
            print(f"  Bob  → {event.final_text[:80]}")

    # Alice asks a follow-up — agent should remember her name from turn 1
    async for event in alice_session.run("Do you remember my name?"):
        if event.type == "result":
            print(f"  Alice→ {event.final_text[:80]}")

    # Bob asks a follow-up — should NOT know Alice's name
    async for event in bob_session.run("Who else are you talking to?"):
        if event.type == "result":
            print(f"  Bob  → {event.final_text[:80]}")


# ── 2. Persistent sessions (SQLite) ──────────────────────────────────────────
#
# Use SqliteSessionStore so history survives process restarts.
# Pass the same session ID to resume an existing conversation.


async def demo_persistent_sessions(db_path: Path) -> None:
    print("\n── 2. Persistent sessions with SQLite ──")

    agent = Agent(
        model=MODEL,
        openai_api_key=API_KEY,
        session_store=SqliteSessionStore(db_path),
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        system_prompt_config=SystemPromptConfig(
            replace_defaults=True,
            append="You are a note-taking assistant. Remember everything the user tells you.",
        ),
        tools=empty_tools(),
    )

    SESSION_ID = "persistent-demo-001"

    # First "process" — create session and save a fact
    session_a = await agent.session(id=SESSION_ID)
    async for event in session_a.run("Remember: my favourite colour is purple."):
        if event.type == "result":
            print(f"  Turn 1: {event.final_text[:80]}")

    # Second "process" — load the same session ID and ask about the fact
    # (In a real app, this would be a new HTTP request or process restart)
    agent2 = Agent(
        model=MODEL,
        openai_api_key=API_KEY,
        session_store=SqliteSessionStore(db_path),
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        system_prompt_config=SystemPromptConfig(
            replace_defaults=True,
            append="You are a note-taking assistant. Remember everything the user tells you.",
        ),
        tools=empty_tools(),
    )
    session_b = await agent2.session(id=SESSION_ID)
    async for event in session_b.run("What is my favourite colour?"):
        if event.type == "result":
            print(f"  Turn 2 (resumed): {event.final_text[:80]}")

    await agent.close()
    await agent2.close()


# ── 3. Concurrent sessions ────────────────────────────────────────────────────
#
# Run several user sessions in parallel with asyncio.gather.
# Each session.run() is an independent async generator — fully concurrent.


async def handle_user(agent: Agent, user: str, question: str) -> tuple[str, str]:
    session = await agent.session(meta={"user": user})
    result = None
    async for event in session.run(question):
        if event.type == "result":
            result = event
    return user, result.final_text or ""


async def demo_concurrent() -> None:
    print("\n── 3. Concurrent sessions ──")

    agent = Agent(
        model=MODEL,
        openai_api_key=API_KEY,
        session_store=InMemorySessionStore(),
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        system_prompt_config=SystemPromptConfig(
            replace_defaults=True,
            append="Answer in one short sentence.",
        ),
        tools=empty_tools(),
    )

    users = [
        ("user_1", "What is the capital of Japan?"),
        ("user_2", "What is 7 times 8?"),
        ("user_3", "Name one planet in our solar system."),
        ("user_4", "What language is spoken in Brazil?"),
    ]

    results = await asyncio.gather(*[handle_user(agent, u, q) for u, q in users])
    for user, answer in results:
        print(f"  {user}: {answer[:80]}")


# ── 4. Per-session deps override ──────────────────────────────────────────────
#
# The Agent has a default deps. Each session can swap it with RunOptions(deps=...).
# Use this for multi-tenant apps where each user has a different DB handle.


class TenantDbTool:
    name = "get_balance"
    description = "Return the account balance for the current user."
    input_schema = {"type": "object", "properties": {}}
    scope = "read"
    parallel_safe = True

    def validate(self, raw: dict) -> dict:
        return raw

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        # ctx.deps is the tenant-specific "database"
        db = ctx.deps or {}
        balance = db.get("balance", "unknown")
        owner = db.get("owner", "unknown")
        return ToolResult(
            content=f"Account owner: {owner}, Balance: ${balance}",
            summary="get_balance",
        )

    def summarize(self, input: dict) -> str:
        return "get_balance()"


async def demo_per_session_deps() -> None:
    print("\n── 4. Per-session deps (tenant isolation) ──")

    # Agent-level default deps (shared connection pool in real apps)
    default_db = {"owner": "default", "balance": 0}

    agent = Agent(
        model=MODEL,
        openai_api_key=API_KEY,
        session_store=InMemorySessionStore(),
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        tools=empty_tools(TenantDbTool()),
        deps=default_db,
        permissions={"mode": "skip-dangerous"},
        system_prompt_config=SystemPromptConfig(
            replace_defaults=True,
            append="You are a banking assistant. Always call get_balance when asked about balance.",
        ),
    )

    tenants = [
        ("alice", {"owner": "Alice Smith", "balance": 1_250.75}),
        ("bob", {"owner": "Bob Jones", "balance": 342.00}),
    ]

    for tenant_name, tenant_db in tenants:
        session = await agent.session(meta={"tenant": tenant_name})
        result = None
        async for event in session.run(
            "What is my account balance?",
            RunOptions(deps=tenant_db),  # ← per-run tenant DB
        ):
            if event.type == "result":
                result = event
        print(f"  {tenant_name}: {result.final_text[:100]}")


# ── 5. Session metadata ───────────────────────────────────────────────────────
#
# Store arbitrary metadata on sessions. Useful for user IDs, timestamps,
# conversation labels — anything you want to query later.


async def demo_session_metadata() -> None:
    print("\n── 5. Session metadata ──")

    store = InMemorySessionStore()
    agent = Agent(
        model=MODEL,
        openai_api_key=API_KEY,
        session_store=store,
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        tools=empty_tools(),
        system_prompt_config=SystemPromptConfig(replace_defaults=True, append="Answer briefly."),
    )

    # Create sessions with metadata
    sessions_created = []
    tenants = [("u001", "pro"), ("u002", "free"), ("u003", "enterprise")]
    for i, (user_id, plan) in enumerate(tenants):
        session = await agent.session(
            meta={"user_id": user_id, "plan": plan, "conversation_index": i}
        )
        sessions_created.append(session)
        print(f"  Created session {session.id[:8]}… for {user_id} ({plan})")

    # Update metadata mid-conversation (e.g. after user upgrades)
    await sessions_created[1].update_meta({"plan": "pro", "upgraded": True})
    print(f"  Updated u002 → plan: {sessions_created[1].meta.get('plan')}")


# ── 6. Web-server handler pattern ─────────────────────────────────────────────
#
# In a FastAPI/Flask app, your endpoint looks like this:
#
#   POST /chat  {"session_id": "...", "message": "...", "user_id": "..."}
#
# The endpoint retrieves or creates a session, streams events, and returns.
# The Agent is a module-level singleton — one per process.

# Module-level singleton (initialised at startup)
_AGENT: Agent | None = None


def get_agent() -> Agent:
    global _AGENT
    if _AGENT is None:
        _AGENT = Agent(
            model=MODEL,
            openai_api_key=API_KEY,
            session_store=SqliteSessionStore(Path("/tmp/demo_sessions.db")),
            features=FeatureFlags(skills=False, subagents=False, mcp=False),
            tools=empty_tools(),
            system_prompt_config=SystemPromptConfig(
                replace_defaults=True,
                append="You are a helpful assistant.",
            ),
        )
    return _AGENT


async def handle_chat_request(
    session_id: str | None,
    message: str,
    user_id: str,
) -> dict:
    """Simulate a web endpoint handler."""
    agent = get_agent()

    # Load existing session or create a new one
    session = await agent.session(
        id=session_id,
        meta={"user_id": user_id},
    )

    # Collect the full response (in a real app, stream these as SSE)
    events_log = []
    final_text = None
    async for event in session.run(message):
        events_log.append({"type": event.type})
        if event.type == "result":
            final_text = event.final_text

    return {
        "session_id": session.id,
        "response": final_text,
        "events_count": len(events_log),
    }


async def demo_web_handler() -> None:
    print("\n── 6. Web-handler pattern ──")

    # Simulate two requests from the same user
    resp1 = await handle_chat_request(None, "What is the speed of light?", "user42")
    print(f"  Request 1: session={resp1['session_id'][:8]}… | {resp1['response'][:80]}")

    resp2 = await handle_chat_request(resp1["session_id"], "And in km/s?", "user42")
    print(f"  Request 2: session={resp2['session_id'][:8]}… | {resp2['response'][:80]}")

    if _AGENT:
        await _AGENT.close()


# ── Main ───────────────────────────────────────────────────────────────────────


async def main() -> None:
    if not API_KEY:
        print("Set OPENAI_API_KEY to run this example.")
        print("Showing patterns without live calls:")
        print("  1. multi_user     – one Agent, N sessions")
        print("  2. persistent     – SqliteSessionStore, resume by ID")
        print("  3. concurrent     – asyncio.gather across sessions")
        print("  4. per-run deps   – RunOptions(deps=tenant_db)")
        print("  5. metadata       – session.meta + update_meta()")
        print("  6. web handler    – module-level Agent singleton")
        return

    await demo_multi_user()
    await demo_concurrent()
    await demo_per_session_deps()
    await demo_session_metadata()
    await demo_web_handler()

    db = Path("/tmp/persistent_demo.db")
    await demo_persistent_sessions(db)
    if db.exists():
        db.unlink()


if __name__ == "__main__":
    asyncio.run(main())
