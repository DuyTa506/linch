"""Integration tests for the recipes package — proves the primitives compose.

NOTE: agent_kit imports inside test functions so tests are robust to
test_hardening.py's sys.modules reset.
"""

from __future__ import annotations

import json

import pytest

# ── Provider helpers ──────────────────────────────────────────────────────────


def _text_provider(text: str):
    from agent_kit.providers.base import BaseProvider
    from agent_kit.types import Usage

    class _P(BaseProvider):
        id = "fake"
        captured: list = []

        def context_window(self, model: str) -> int:
            return 128_000

        async def stream(self, req):
            self.captured.append(req)
            yield {"type": "message_start", "model": req.model}
            yield {"type": "text_delta", "text": text}
            yield {
                "type": "message_end",
                "stop_reason": "end_turn",
                "usage": Usage(),
                "provider_metadata": None,
            }

    return _P()


def _tool_use_provider(tool_name: str, tool_input: dict):
    from agent_kit.providers.base import BaseProvider
    from agent_kit.types import Usage

    class _P(BaseProvider):
        id = "fake"
        captured: list = []
        _call = 0

        def context_window(self, model: str) -> int:
            return 128_000

        async def stream(self, req):
            self.captured.append(req)
            self._call += 1
            yield {"type": "message_start", "model": req.model}
            if self._call == 1:
                yield {"type": "tool_use_start", "id": "t1", "name": tool_name}
                yield {
                    "type": "tool_use_input_delta",
                    "id": "t1",
                    "json_delta": json.dumps(tool_input),
                }
                yield {"type": "tool_use_end", "id": "t1"}
                yield {
                    "type": "message_end",
                    "stop_reason": "tool_use",
                    "usage": Usage(),
                    "provider_metadata": None,
                }
            else:
                yield {"type": "text_delta", "text": "done"}
                yield {
                    "type": "message_end",
                    "stop_reason": "end_turn",
                    "usage": Usage(),
                    "provider_metadata": None,
                }

    return _P()


# ── RAG agent ────────────────────────────────────────────────────────────────


def test_rag_agent_has_no_swe_tools():
    from agent_kit.recipes.rag import rag_agent

    agent = rag_agent(model="gpt-5", provider=_text_provider("{}"))
    tool_names = {t.name for t in agent.tools.list()}
    swe_tools = {"Bash", "Edit", "Write", "Read", "Glob", "Grep"}
    assert not (tool_names & swe_tools), f"Unexpected SWE tools: {tool_names & swe_tools}"


def test_rag_agent_no_swe_protocol_in_system():
    from agent_kit.recipes.rag import rag_agent

    agent = rag_agent(model="gpt-5", provider=_text_provider("{}"))
    combined = "\n".join(b.text for b in agent.system_blocks)
    assert "Edit" not in combined
    assert "Bash" not in combined


@pytest.mark.asyncio
async def test_rag_agent_structured_output():
    from agent_kit.recipes.rag import rag_agent
    from agent_kit.sessions import InMemorySessionStore

    payload = {"answer": "42 days", "citations": ["doc1"]}
    provider = _text_provider(json.dumps(payload))

    agent = rag_agent(model="gpt-5", provider=provider, session_store=InMemorySessionStore())
    session = await agent.session()
    result = None
    async for event in session.run("How long?"):
        if event.type == "result":
            result = event

    assert result is not None
    assert result.structured_output == payload


# ── text-to-SQL agent ─────────────────────────────────────────────────────────

_SCHEMA = "CREATE TABLE users (id INT, name TEXT, age INT);"
_SQL_OUTPUT = {"sql": "SELECT COUNT(*) FROM users WHERE age > 30", "rationale": "Count adults."}


@pytest.mark.asyncio
async def test_sql_agent_terminates_on_emit_sql():
    from agent_kit.recipes.text_to_sql import sql_agent
    from agent_kit.sessions import InMemorySessionStore

    provider = _tool_use_provider("emit_sql", _SQL_OUTPUT)
    agent = sql_agent(
        model="gpt-5", schema=_SCHEMA, provider=provider, session_store=InMemorySessionStore()
    )
    session = await agent.session()
    result = None
    tool_ends = []
    async for event in session.run("How many adults?"):
        if event.type == "result":
            result = event
        if event.type == "tool_call_end":
            tool_ends.append(event)

    assert result is not None
    assert result.structured_output == _SQL_OUTPUT
    assert tool_ends == []


def test_sql_agent_system_contains_schema():
    from agent_kit.recipes.text_to_sql import sql_agent

    agent = sql_agent(model="gpt-5", schema=_SCHEMA, provider=_text_provider("{}"))
    combined = "\n".join(b.text for b in agent.system_blocks)
    assert "users" in combined
    assert "CREATE TABLE" in combined


def test_sql_agent_has_emit_sql_tool():
    from agent_kit.recipes.text_to_sql import sql_agent

    agent = sql_agent(model="gpt-5", schema=_SCHEMA, provider=_text_provider("{}"))
    assert agent.tools.get("emit_sql") is not None


# ── document-analysis agent ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_doc_agent_structured_output():
    from agent_kit import RunOptions
    from agent_kit.recipes.doc_analysis import doc_agent
    from agent_kit.sessions import InMemorySessionStore

    payload = {"entities": [{"type": "date", "value": "2024-01-01"}], "summary": "An invoice."}
    provider = _text_provider(json.dumps(payload))

    agent = doc_agent(model="gpt-5", provider=provider, session_store=InMemorySessionStore())
    session = await agent.session()
    result = None
    async for event in session.run(
        "Extract all dates.",
        RunOptions(images=[{"url": "https://example.com/doc.png"}]),
    ):
        if event.type == "result":
            result = event

    assert result is not None
    assert result.structured_output == payload


def test_doc_agent_no_swe_protocol():
    from agent_kit.recipes.doc_analysis import doc_agent

    agent = doc_agent(model="gpt-5", provider=_text_provider("{}"))
    combined = "\n".join(b.text for b in agent.system_blocks)
    assert "Bash" not in combined
    assert "Edit" not in combined


# ── build_agent scaffold ──────────────────────────────────────────────────────


def test_build_agent_custom_domain():
    from agent_kit.recipes import build_agent
    from agent_kit.tools.base import ToolContext, ToolResult
    from agent_kit.tools.registry import empty_tools

    class MyTool:
        name = "Search"
        description = "Search the web."
        input_schema: dict = {"type": "object", "properties": {}}
        scope = "read"
        parallel_safe = True

        def validate(self, raw):
            return raw

        async def execute(self, input, ctx: ToolContext) -> ToolResult:
            return ToolResult(content="results", summary="Search")

        def summarize(self, input):
            return "Search"

    agent = build_agent(
        model="gpt-5",
        provider=_text_provider("custom"),
        system_instructions="You are a helpful research assistant.",
        tools=empty_tools(MyTool()),
        replace_default_system=True,
    )
    combined = "\n".join(b.text for b in agent.system_blocks)
    assert "research assistant" in combined
    assert "autonomous software engineering" not in combined
    assert agent.tools.get("Search") is not None
