"""Structured output — all patterns.

Run:
    OPENAI_API_KEY=sk-... python examples/04_structured_output.py

Demonstrates:
  1. OutputSchema on the Agent (agent-wide default)
  2. OutputSchema on RunOptions (per-run override)
  3. final_tool_name — terminal tool pattern (provider-agnostic)
  4. structured_error handling — when the model returns bad JSON
  5. Nested schema — extract complex documents
  6. SQL generation — forced structured output
  7. Classification — choose from an enum
"""

from __future__ import annotations

import asyncio
import os

from agent_kit import Agent, RunOptions
from agent_kit.config import FeatureFlags, SystemPromptConfig
from agent_kit.sessions import InMemorySessionStore
from agent_kit.tools.base import ToolContext, ToolResult
from agent_kit.tools.registry import empty_tools
from agent_kit.types import OutputSchema

API_KEY = os.environ.get("OPENAI_API_KEY", "")
MODEL = "gpt-5-nano-2025-08-07"

BASE = dict(
    model=MODEL,
    openai_api_key=API_KEY,
    session_store=InMemorySessionStore(),
    features=FeatureFlags(skills=False, subagents=False, mcp=False),
    permissions={"mode": "skip-dangerous"},
)


# ── 1. Agent-wide OutputSchema ────────────────────────────────────────────────
#
# Every run on this agent returns the same JSON shape.
# Good for: a dedicated extraction service, a classification endpoint.

SENTIMENT_SCHEMA = OutputSchema(
    name="sentiment",
    schema={
        "type": "object",
        "properties": {
            "label": {"type": "string", "enum": ["positive", "negative", "neutral"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "reasoning": {"type": "string"},
        },
        "required": ["label", "confidence", "reasoning"],
        "additionalProperties": False,
    },
    strict=True,
)


def make_sentiment_agent() -> Agent:
    return Agent(
        **BASE,
        tools=empty_tools(),
        output_schema=SENTIMENT_SCHEMA,
        system_prompt_config=SystemPromptConfig(
            replace_defaults=True,
            append=(
                "You are a sentiment analysis engine. "
                "Classify the sentiment of the user's text. "
                "Respond ONLY with a JSON object — no markdown, no prose."
            ),
        ),
    )


# ── 2. Per-run OutputSchema via RunOptions ────────────────────────────────────
#
# Same Agent, different schema per call. Useful when you want to reuse the
# model config but extract different shapes on each request.

async def demo_per_run_schema(agent: Agent) -> None:
    print("\n── 2. Per-run schema via RunOptions ──")

    # First run: extract entities
    entity_schema = OutputSchema(
        name="entities",
        schema={
            "type": "object",
            "properties": {
                "people": {"type": "array", "items": {"type": "string"}},
                "places": {"type": "array", "items": {"type": "string"}},
                "organizations": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["people", "places", "organizations"],
            "additionalProperties": False,
        },
        strict=True,
    )

    text = "Elon Musk founded SpaceX in Hawthorne, California, while working at Tesla."
    session = await agent.session()
    result = None
    async for event in session.run(
        f"Extract named entities from: {text}",
        RunOptions(output_schema=entity_schema),
    ):
        if event.type == "result":
            result = event
    print("  Entities:", result.structured_output)

    # Second run: key-value pairs
    kv_schema = OutputSchema(
        name="key_value_pairs",
        schema={
            "type": "object",
            "properties": {
                "pairs": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "key": {"type": "string"},
                            "value": {"type": "string"},
                        },
                        "required": ["key", "value"],
                    },
                }
            },
            "required": ["pairs"],
            "additionalProperties": False,
        },
    )
    session2 = await agent.session()
    result2 = None
    async for event in session2.run(
        "Extract key-value pairs from: Name=Alice, Age=30, City=Berlin",
        RunOptions(output_schema=kv_schema),
    ):
        if event.type == "result":
            result2 = event
    print("  KV pairs:", result2.structured_output)


# ── 3. final_tool_name — terminal tool (provider-agnostic) ───────────────────
#
# Instead of relying on response_format (OpenAI-only), register a tool whose
# input_schema IS your output schema. Set final_tool_name to its name.
# The loop intercepts the call, sets ResultEvent.structured_output = tool_input,
# and returns without executing the tool. Works with ANY provider (Anthropic too).

def make_sql_agent(db_schema: str) -> Agent:
    sql_output_schema = OutputSchema(
        name="emit_sql",
        schema={
            "type": "object",
            "properties": {
                "sql": {"type": "string"},
                "explanation": {"type": "string"},
                "tables_used": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["sql", "explanation", "tables_used"],
            "additionalProperties": False,
        },
        strict=True,
    )

    class EmitSqlTool:
        """Terminal tool — never executed, just captures the model's output."""
        name = "emit_sql"
        description = (
            "Output the final SQL query. Call this ONCE when you are ready. "
            "The system will return your input directly — do not call any other tool after this."
        )
        input_schema = sql_output_schema.schema
        scope = "read"
        parallel_safe = False

        def validate(self, raw: dict) -> dict:
            return raw

        async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
            # Never reached — loop intercepts this tool.
            return ToolResult(content="(terminal)", summary="emit_sql")

        def summarize(self, input: dict) -> str:
            return f"emit_sql({input.get('sql', '')[:60]})"

    return Agent(
        **BASE,
        tools=empty_tools(EmitSqlTool()),
        output_schema=sql_output_schema,
        final_tool_name="emit_sql",
        system_prompt_config=SystemPromptConfig(
            replace_defaults=True,
            append=(
                f"You are a SQL assistant.\n\nDatabase schema:\n```sql\n{db_schema}\n```\n\n"
                "When you know the answer, call emit_sql with the query, explanation, and "
                "list of tables used. Do NOT answer in plain text."
            ),
        ),
    )


# ── 4. structured_error handling ─────────────────────────────────────────────
#
# When the model ignores the schema and returns prose, structured_output is None
# and structured_error has the parse failure reason. Handle gracefully.

async def demo_error_handling(agent: Agent) -> None:
    print("\n── 4. structured_error handling ──")
    session = await agent.session()
    result = None
    async for event in session.run("Tell me a joke."):  # off-topic; may not produce JSON
        if event.type == "result":
            result = event

    if result.structured_output:
        print("  Got structured output:", result.structured_output)
    elif result.structured_error:
        print("  Parse failed:", result.structured_error[:100])
        print("  Raw text:", (result.final_text or "")[:100])
    else:
        print("  No schema set — plain text:", result.final_text)


# ── 5. Complex nested schema — document extraction ────────────────────────────

INVOICE_SCHEMA = OutputSchema(
    name="invoice",
    schema={
        "type": "object",
        "properties": {
            "invoice_number": {"type": "string"},
            "date": {"type": "string"},
            "vendor": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "address": {"type": "string"},
                },
                "required": ["name", "address"],
            },
            "line_items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "description": {"type": "string"},
                        "quantity": {"type": "integer"},
                        "unit_price": {"type": "number"},
                        "total": {"type": "number"},
                    },
                    "required": ["description", "quantity", "unit_price", "total"],
                },
            },
            "subtotal": {"type": "number"},
            "tax": {"type": "number"},
            "total_due": {"type": "number"},
        },
        "required": ["invoice_number", "date", "vendor", "line_items", "subtotal", "total_due"],
        "additionalProperties": False,
    },
    strict=True,
)

SAMPLE_INVOICE = """
Invoice #INV-2024-001
Date: 2024-03-15
Vendor: Acme Corp, 123 Main St, Springfield

Items:
- Widget A (x10) @ $5.00 each = $50.00
- Widget B (x3)  @ $25.00 each = $75.00

Subtotal: $125.00
Tax (10%): $12.50
Total Due: $137.50
"""


def make_invoice_extractor() -> Agent:
    return Agent(
        **BASE,
        tools=empty_tools(),
        output_schema=INVOICE_SCHEMA,
        system_prompt_config=SystemPromptConfig(
            replace_defaults=True,
            append=(
                "You are an invoice extraction service. "
                "Extract all invoice fields from the provided text. "
                "Return ONLY a JSON object matching the schema — no prose."
            ),
        ),
    )


# ── 6. Classification ─────────────────────────────────────────────────────────

TICKET_SCHEMA = OutputSchema(
    name="ticket_classification",
    schema={
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": ["billing", "technical", "account", "feature_request", "other"],
            },
            "priority": {"type": "string", "enum": ["low", "medium", "high", "urgent"]},
            "summary": {"type": "string", "maxLength": 100},
            "requires_human": {"type": "boolean"},
        },
        "required": ["category", "priority", "summary", "requires_human"],
        "additionalProperties": False,
    },
    strict=True,
)


def make_ticket_classifier() -> Agent:
    return Agent(
        **BASE,
        tools=empty_tools(),
        output_schema=TICKET_SCHEMA,
        system_prompt_config=SystemPromptConfig(
            replace_defaults=True,
            append=(
                "You are a support ticket classifier. "
                "Given a support ticket, output ONLY a JSON object with category, priority, "
                "one-sentence summary, and whether it requires a human agent."
            ),
        ),
    )


# ── Live demos ─────────────────────────────────────────────────────────────────


async def main() -> None:
    if not API_KEY:
        print("Set OPENAI_API_KEY to run this example.")
        print("Schemas validated:")
        for name, schema in [
            ("sentiment", SENTIMENT_SCHEMA),
            ("invoice", INVOICE_SCHEMA),
            ("ticket", TICKET_SCHEMA),
        ]:
            print(f"  {name}: {list(schema.schema['properties'].keys())}")
        return

    # Demo 1: Sentiment
    print("\n── 1. Sentiment analysis ──")
    agent = make_sentiment_agent()
    for text in [
        "I absolutely love this product, it changed my life!",
        "The shipping was delayed and customer service was unhelpful.",
    ]:
        session = await agent.session()
        result = None
        async for event in session.run(text):
            if event.type == "result":
                result = event
        print(f"  '{text[:40]}…' → {result.structured_output}")

    # Demo 2: Per-run schema
    general_agent = Agent(
        **BASE,
        tools=empty_tools(),
        system_prompt_config=SystemPromptConfig(
            replace_defaults=True,
            append="Extract structured data from user input. Respond ONLY with JSON.",
        ),
    )
    await demo_per_run_schema(general_agent)

    # Demo 3: SQL with final_tool_name
    print("\n── 3. SQL generation with final_tool_name ──")
    schema_sql = "CREATE TABLE products (id INT, name TEXT, price FLOAT, stock INT, category TEXT);"
    sql_agent = make_sql_agent(schema_sql)
    session = await sql_agent.session()
    result = None
    async for event in session.run("Which product categories have average price above $50?"):
        if event.type == "result":
            result = event
    print("  SQL:", result.structured_output.get("sql"))
    print("  Explanation:", result.structured_output.get("explanation"))
    print("  Tables:", result.structured_output.get("tables_used"))

    # Demo 4: Invoice extraction
    print("\n── 4. Invoice extraction (nested schema) ──")
    inv_agent = make_invoice_extractor()
    session = await inv_agent.session()
    result = None
    async for event in session.run(f"Extract the invoice:\n{SAMPLE_INVOICE}"):
        if event.type == "result":
            result = event
    if result.structured_output is not None:
        out = result.structured_output
        print(f"  Invoice #{out.get('invoice_number')} from {out.get('vendor', {}).get('name')}")
        print(f"  Total due: ${out.get('total_due')}")
        print(f"  Line items: {len(out.get('line_items', []))}")
    else:
        print("  Parse error:", result.structured_error)
        print("  Raw text:", (result.final_text or "")[:200])

    # Demo 5: Ticket classification
    print("\n── 5. Ticket classification ──")
    classifier = make_ticket_classifier()
    tickets = [
        "I can't log in to my account — it says my password is wrong but I just reset it.",
        "Would love to see dark mode in the mobile app.",
        "You charged me twice this month, I need a refund ASAP!",
    ]
    for ticket in tickets:
        session = await classifier.session()
        result = None
        async for event in session.run(ticket):
            if event.type == "result":
                result = event
        out = result.structured_output or {}
        cat = out.get('category', '?')
        pri = out.get('priority', '?')
        summ = out.get('summary', '?')[:50]
        print(f"  [{pri:6}] {cat:16} | {summ}")


if __name__ == "__main__":
    asyncio.run(main())
