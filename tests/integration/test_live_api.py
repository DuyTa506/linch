"""Live API integration tests — require OPENAI_API_KEY to be set.

Run with:
    OPENAI_API_KEY=<key> pytest tests/test_live_api.py -v

These tests exercise the real OpenAI Responses API with gpt-5-nano-2025-08-07.
They verify that each new harness primitive works end-to-end, not just in
isolation with a fake provider.

Skipped automatically when OPENAI_API_KEY is absent.
"""

from __future__ import annotations

import os

import pytest

MODEL = "gpt-5-nano-2025-08-07"
SKIP_REASON = "OPENAI_API_KEY not set"
needs_key = pytest.mark.skipif(not os.environ.get("OPENAI_API_KEY"), reason=SKIP_REASON)


# ── 1. Basic text completion ──────────────────────────────────────────────────


@needs_key
@pytest.mark.asyncio
async def test_live_basic_completion():
    """The agent returns a text response for a simple prompt."""
    from linch import Agent
    from linch.config import FeatureFlags, SystemPromptConfig
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools

    agent = Agent(
        model=MODEL,
        system_prompt_config=SystemPromptConfig(
            replace_defaults=True,
            append="You are a concise assistant. Reply in one sentence.",
        ),
        tools=empty_tools(),
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        session_store=InMemorySessionStore(),
    )
    session = await agent.session()
    results = []
    async for event in session.run("What is 2 + 2?"):
        if event.type == "result":
            results.append(event)

    assert results, "no result event"
    assert results[0].subtype == "success"
    assert results[0].final_text is not None
    assert "4" in results[0].final_text


# ── 2. Structured output via OutputSchema ─────────────────────────────────────


@needs_key
@pytest.mark.asyncio
async def test_live_structured_output():
    """OutputSchema produces a parsed dict in ResultEvent.structured_output."""
    from linch import Agent
    from linch.config import FeatureFlags, SystemPromptConfig
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools
    from linch.types import OutputSchema

    schema = OutputSchema(
        name="capital_info",
        schema={
            "type": "object",
            "properties": {
                "country": {"type": "string"},
                "capital": {"type": "string"},
                "population_approx": {"type": "integer"},
            },
            "required": ["country", "capital", "population_approx"],
            "additionalProperties": False,
        },
        strict=True,
    )

    agent = Agent(
        model=MODEL,
        system_prompt_config=SystemPromptConfig(
            replace_defaults=True,
            append=(
                "You are a geography assistant. "
                "Always respond with a valid JSON object that matches the requested schema. "
                "No markdown fences — raw JSON only."
            ),
        ),
        tools=empty_tools(),
        output_schema=schema,
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        session_store=InMemorySessionStore(),
    )
    session = await agent.session()
    result = None
    async for event in session.run("Give me info about France."):
        if event.type == "result":
            result = event

    assert result is not None, "no result event"
    assert result.subtype == "success"
    assert result.structured_error is None, f"parse error: {result.structured_error}"
    assert result.structured_output is not None, "structured_output is None"

    out = result.structured_output
    assert isinstance(out, dict)
    assert "capital" in out
    assert "Paris" in out["capital"] or "paris" in out["capital"].lower()
    assert out["country"].lower() in ("france", "french republic")
    assert isinstance(out["population_approx"], int)
    assert out["population_approx"] > 1_000_000


# ── 3. final_tool_name — terminal tool (SQL generation) ───────────────────────


@needs_key
@pytest.mark.asyncio
async def test_live_final_tool_sql():
    """final_tool_name returns structured output without executing the tool."""
    from linch import Agent
    from linch.config import FeatureFlags, SystemPromptConfig
    from linch.sessions import InMemorySessionStore
    from linch.tools.base import ToolContext, ToolResult
    from linch.tools.registry import empty_tools
    from linch.types import OutputSchema

    sql_schema = OutputSchema(
        name="emit_sql",
        schema={
            "type": "object",
            "properties": {
                "sql": {"type": "string"},
                "rationale": {"type": "string"},
            },
            "required": ["sql", "rationale"],
            "additionalProperties": False,
        },
        strict=True,
    )

    # The emit_sql tool is terminal — it should NEVER be executed
    tool_was_executed = []

    class EmitSqlTool:
        name = "emit_sql"
        description = (
            "Emit the final SQL query. Call this once when you are confident. "
            "The system will return your input directly as the structured result."
        )
        input_schema = sql_schema.schema
        scope = "read"
        parallel_safe = False

        def validate(self, raw):
            return raw

        async def execute(self, input, ctx: ToolContext) -> ToolResult:
            tool_was_executed.append(input)
            return ToolResult(content="executed (should not happen)", summary="emit_sql")

        def summarize(self, input):
            return f"emit_sql({input.get('sql', '')[:40]})"

    db_schema = "CREATE TABLE sales (id INT, product TEXT, amount FLOAT, region TEXT);"

    agent = Agent(
        model=MODEL,
        system_prompt_config=SystemPromptConfig(
            replace_defaults=True,
            append=(
                f"You are a SQL assistant. Database schema:\n```sql\n{db_schema}\n```\n\n"
                "When you know the answer, call the emit_sql tool with the SQL "
                "query and a brief rationale. "
                "Do NOT answer in plain text — always call emit_sql."
            ),
        ),
        tools=empty_tools(EmitSqlTool()),
        output_schema=sql_schema,
        final_tool_name="emit_sql",
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        session_store=InMemorySessionStore(),
    )
    session = await agent.session()
    result = None
    tool_call_end_events = []
    async for event in session.run("Show me total sales by region."):
        if event.type == "result":
            result = event
        elif event.type == "tool_call_end":
            tool_call_end_events.append(event)

    assert result is not None
    assert result.subtype == "success", f"got subtype={result.subtype}"
    assert result.structured_output is not None, "structured_output is None"
    out = result.structured_output
    assert "sql" in out
    assert "select" in out["sql"].lower() or "SELECT" in out["sql"]
    assert "region" in out["sql"].lower()
    assert "rationale" in out

    # The terminal tool must NOT be dispatched for real execution
    assert tool_was_executed == [], "emit_sql.execute() was called — should not happen"
    # No tool_call_end event for the terminal tool
    assert tool_call_end_events == [], "tool_call_end emitted — should not happen"


# ── 4. ToolContext.deps — shared app state ─────────────────────────────────────


@needs_key
@pytest.mark.asyncio
async def test_live_tool_deps():
    """ctx.deps is available in tools and carries app-level state."""
    from linch import Agent
    from linch.config import FeatureFlags, SystemPromptConfig
    from linch.sessions import InMemorySessionStore
    from linch.tools.base import ToolContext, ToolResult
    from linch.tools.registry import empty_tools

    # In-memory knowledge base injected via deps
    kb = {
        "return_policy": "Returns accepted within 30 days with receipt.",
        "shipping": "Free shipping on orders over $50.",
        "hours": "Support available Monday–Friday, 9 AM–6 PM EST.",
    }

    deps_received_in_tool = []

    class LookupTool:
        name = "lookup_kb"
        description = "Look up an entry from the knowledge base by key."
        input_schema = {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Knowledge base key to look up.",
                    "enum": ["return_policy", "shipping", "hours"],
                }
            },
            "required": ["key"],
        }
        scope = "read"
        parallel_safe = True

        def validate(self, raw):
            if "key" not in raw:
                raise ValueError("key is required")
            return raw

        async def execute(self, input, ctx: ToolContext) -> ToolResult:
            deps_received_in_tool.append(ctx.deps)
            kb_dict = ctx.deps  # the dict passed as Agent(deps=...)
            value = kb_dict.get(input["key"], "Not found.")
            return ToolResult(content=value, summary=f"lookup_kb({input['key']})")

        def summarize(self, input):
            return f"lookup_kb({input.get('key', '?')})"

    agent = Agent(
        model=MODEL,
        system_prompt_config=SystemPromptConfig(
            replace_defaults=True,
            append=(
                "You are a customer support assistant. "
                "Use the lookup_kb tool to retrieve accurate policy information before answering. "
                "Always call the tool before responding."
            ),
        ),
        tools=empty_tools(LookupTool()),
        deps=kb,
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
    )
    session = await agent.session()
    result = None
    async for event in session.run("What is your return policy?"):
        if event.type == "result":
            result = event

    assert result is not None
    assert result.subtype == "success"
    assert result.final_text is not None
    assert "30" in result.final_text or "return" in result.final_text.lower()

    # Tool received the kb dict as deps
    assert len(deps_received_in_tool) >= 1, "tool was never called"
    assert deps_received_in_tool[0] is kb, "ctx.deps is not the kb dict"


# ── 5. Context building (RAG-per-turn) ────────────────────────────────────────


@needs_key
@pytest.mark.asyncio
async def test_live_context_injection():
    """ContextBuilder injects retrieved docs into provider requests before each turn."""
    from linch import Agent
    from linch.config import FeatureFlags, SystemPromptConfig
    from linch.context import ContextBuildResult
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools
    from linch.types import Message, TextBlock

    TAG = "[[docs]]"
    builder_called_with_deps = []

    # Minimal fake vector store — just a dict keyed by topic
    class FakeVectorStore:
        _docs = {
            "weather": "The weather in Tokyo today is 22°C and partly cloudy.",
            "news": "Tech stocks rose 2% in morning trading.",
        }

        def search(self, query: str) -> str:
            hits = [v for k, v in self._docs.items() if k in query.lower()]
            return " | ".join(hits) if hits else ""

    store = FakeVectorStore()

    class RagContextBuilder:
        async def build(self, turn) -> ContextBuildResult:
            builder_called_with_deps.append(turn.deps)
            # Retrieve from deps (the vector store passed as Agent(deps=...)).
            vs = turn.deps
            last_text = ""
            for msg in reversed(turn.messages):
                if msg.role == "user":
                    for blk in msg.content:
                        if isinstance(blk, TextBlock) and not blk.text.startswith("<env>"):
                            last_text = blk.text
                            break
                if last_text:
                    break
            docs = vs.search(last_text)
            if docs:
                return ContextBuildResult(
                    messages=[
                        Message(
                            role="user",
                            content=[TextBlock(text=f"{TAG} Retrieved: {docs}")],
                        )
                    ]
                )
            return ContextBuildResult()

    agent = Agent(
        model=MODEL,
        system_prompt_config=SystemPromptConfig(
            replace_defaults=True,
            append=(
                "You are a helpful assistant. "
                "Use the retrieved context provided before each turn to answer accurately."
            ),
        ),
        tools=empty_tools(),
        context_builder=RagContextBuilder(),
        deps=store,
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        session_store=InMemorySessionStore(),
    )
    session = await agent.session()
    result = None
    async for event in session.run("What is the weather in Tokyo?"):
        if event.type == "result":
            result = event

    assert result is not None
    assert result.subtype == "success"
    assert result.final_text is not None
    # The injected weather doc should have been used
    assert "22" in result.final_text or "tokyo" in result.final_text.lower()

    # Builder was called and received the vector store as deps.
    assert len(builder_called_with_deps) >= 1
    assert builder_called_with_deps[0] is store


# ── 6. FeatureFlags + SystemPromptConfig.replace_defaults ─────────────────────


@needs_key
@pytest.mark.asyncio
async def test_live_custom_system_prompt():
    """replace_defaults=True fully replaces the SWE identity with a custom prompt."""
    from linch import Agent
    from linch.config import FeatureFlags, SystemPromptConfig
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools

    agent = Agent(
        model=MODEL,
        system_prompt_config=SystemPromptConfig(
            replace_defaults=True,
            append=(
                "You are ARIA, an AI assistant specialized in cooking. "
                "Always start your response with 'ARIA: '."
            ),
        ),
        tools=empty_tools(),
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        session_store=InMemorySessionStore(),
    )

    # Verify system blocks contain no SWE content
    combined_system = "\n".join(b.text for b in agent.system_blocks)
    assert "software engineering" not in combined_system
    assert "ARIA" in combined_system

    session = await agent.session()
    result = None
    async for event in session.run("How do I make pasta?"):
        if event.type == "result":
            result = event

    assert result is not None
    assert result.subtype == "success"
    assert result.final_text is not None
    # The model should follow the ARIA persona
    assert "ARIA" in result.final_text or "pasta" in result.final_text.lower()


# ── 7. RunOptions.deps override ────────────────────────────────────────────────


@needs_key
@pytest.mark.asyncio
async def test_live_run_options_deps_override():
    """RunOptions.deps overrides Agent.deps for a specific run."""
    from linch import Agent, RunOptions
    from linch.config import FeatureFlags, SystemPromptConfig
    from linch.sessions import InMemorySessionStore
    from linch.tools.base import ToolContext, ToolResult
    from linch.tools.registry import empty_tools

    # Two different "databases" we'll swap between runs
    db_a = {"name": "Database A", "record_count": 1000}
    db_b = {"name": "Database B", "record_count": 5000}

    deps_seen = []

    class DbInfoTool:
        name = "get_db_info"
        description = "Returns information about the current database."
        input_schema = {"type": "object", "properties": {}}
        scope = "read"
        parallel_safe = True

        def validate(self, raw):
            return raw

        async def execute(self, input, ctx: ToolContext) -> ToolResult:
            deps_seen.append(ctx.deps)
            db = ctx.deps
            info = f"Database: {db['name']}, Records: {db['record_count']}"
            return ToolResult(content=info, summary="get_db_info")

        def summarize(self, input):
            return "get_db_info"

    agent = Agent(
        model=MODEL,
        system_prompt_config=SystemPromptConfig(
            replace_defaults=True,
            append=(
                "You are a database assistant. Call get_db_info to answer "
                "questions about the database."
            ),
        ),
        tools=empty_tools(DbInfoTool()),
        deps=db_a,  # agent-level default
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
    )

    # Run 1: uses agent-level deps (db_a)
    session_a = await agent.session()
    result_a = None
    async for event in session_a.run("What database am I connected to?"):
        if event.type == "result":
            result_a = event

    # Run 2: override with db_b via RunOptions
    session_b = await agent.session()
    result_b = None
    async for event in session_b.run("What database am I connected to?", RunOptions(deps=db_b)):
        if event.type == "result":
            result_b = event

    assert result_a is not None and result_a.subtype == "success"
    assert result_b is not None and result_b.subtype == "success"

    # Check which deps each run saw in the tool
    run_a_deps = [d for d in deps_seen if d.get("name") == "Database A"]
    run_b_deps = [d for d in deps_seen if d.get("name") == "Database B"]
    assert len(run_a_deps) >= 1, f"run A never saw db_a. deps_seen={deps_seen}"
    assert len(run_b_deps) >= 1, f"run B never saw db_b. deps_seen={deps_seen}"
