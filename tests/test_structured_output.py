"""Tests for structured output (OutputSchema + tool_choice + final_tool_name).

NOTE: agent_kit imports inside test functions (not module-level) so tests are
robust to test_hardening.py's sys.modules reset.
"""

from __future__ import annotations

import json

import pytest

# ── Provider helpers (lazy imports inside) ───────────────────────────────────


def _text_provider(text: str):
    from agent_kit.providers.base import BaseProvider
    from agent_kit.types import Usage

    class _Provider(BaseProvider):
        id = "fake"
        captured_reqs: list = []

        def context_window(self, model: str) -> int:
            return 128_000

        async def stream(self, req):
            self.captured_reqs.append(req)
            yield {"type": "message_start", "model": req.model}
            yield {"type": "text_delta", "text": text}
            yield {
                "type": "message_end",
                "stop_reason": "end_turn",
                "usage": Usage(),
                "provider_metadata": None,
            }

    return _Provider()


def _tool_use_provider(tool_name: str, tool_input: dict):
    from agent_kit.providers.base import BaseProvider
    from agent_kit.types import Usage

    class _Provider(BaseProvider):
        id = "fake"
        captured_reqs: list = []
        _call = 0

        def context_window(self, model: str) -> int:
            return 128_000

        async def stream(self, req):
            self.captured_reqs.append(req)
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

    return _Provider()


def _make_emit_tool():
    from agent_kit.tools.base import ToolContext, ToolResult

    class _EmitTool:
        name = "emit_answer"
        description = "Final answer tool."
        input_schema: dict = {
            "type": "object",
            "properties": {"sql": {"type": "string"}},
            "required": ["sql"],
        }
        scope = "read"
        parallel_safe = False

        def validate(self, raw: dict) -> dict:
            return raw

        async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
            return ToolResult(content="should not run", summary="emit_answer")

        def summarize(self, input: dict) -> str:
            return "emit_answer"

    return _EmitTool()


# ── OutputSchema ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_structured_output_from_json_text():
    from agent_kit import Agent
    from agent_kit.sessions import InMemorySessionStore
    from agent_kit.tools.registry import empty_tools
    from agent_kit.types import OutputSchema

    payload = {"answer": "42", "confidence": 0.9}
    provider = _text_provider(json.dumps(payload))

    schema = OutputSchema(
        name="test_schema",
        schema={
            "type": "object",
            "properties": {
                "answer": {"type": "string"},
                "confidence": {"type": "number"},
            },
        },
    )
    agent = Agent(
        model="gpt-5",
        provider=provider,
        tools=empty_tools(),
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        output_schema=schema,
    )
    session = await agent.session()
    result = None
    async for event in session.run("go"):
        if event.type == "result":
            result = event

    assert result is not None
    assert result.structured_output == payload
    assert result.structured_error is None


@pytest.mark.asyncio
async def test_structured_output_malformed_json():
    from agent_kit import Agent
    from agent_kit.sessions import InMemorySessionStore
    from agent_kit.tools.registry import empty_tools
    from agent_kit.types import OutputSchema

    provider = _text_provider("not valid json {{")

    schema = OutputSchema(name="s", schema={"type": "object"})
    agent = Agent(
        model="gpt-5",
        provider=provider,
        tools=empty_tools(),
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        output_schema=schema,
    )
    session = await agent.session()
    result = None
    async for event in session.run("go"):
        if event.type == "result":
            result = event

    assert result is not None
    assert result.structured_output is None
    assert result.structured_error is not None
    assert "JSON" in result.structured_error


@pytest.mark.asyncio
async def test_no_output_schema_structured_output_is_none():
    from agent_kit import Agent
    from agent_kit.sessions import InMemorySessionStore
    from agent_kit.tools.registry import empty_tools

    provider = _text_provider('{"key": "value"}')

    agent = Agent(
        model="gpt-5",
        provider=provider,
        tools=empty_tools(),
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
    )
    session = await agent.session()
    result = None
    async for event in session.run("go"):
        if event.type == "result":
            result = event

    assert result is not None
    assert result.structured_output is None
    assert result.structured_error is None


# ── Chat provider payload ─────────────────────────────────────────────────────


def test_chat_payload_response_format():
    from agent_kit.providers.openai_chat import _build_chat_payload
    from agent_kit.types import OutputSchema, ProviderRequest

    schema = OutputSchema(name="my_schema", schema={"type": "object"})
    req = ProviderRequest(
        model="gpt-5",
        system=[],
        tools=[],
        messages=[],
        output_schema=schema,
    )
    payload = _build_chat_payload(req)
    assert "response_format" in payload
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["response_format"]["json_schema"]["name"] == "my_schema"


def test_chat_payload_no_schema_no_response_format():
    from agent_kit.providers.openai_chat import _build_chat_payload
    from agent_kit.types import ProviderRequest

    req = ProviderRequest(model="gpt-5", system=[], tools=[], messages=[])
    payload = _build_chat_payload(req)
    assert "response_format" not in payload


# ── Responses provider payload ────────────────────────────────────────────────


def test_responses_payload_text_format():
    from agent_kit.openai_responses import build_payload
    from agent_kit.types import OutputSchema, ProviderRequest

    schema = OutputSchema(name="my_schema", schema={"type": "object"}, strict=True)
    req = ProviderRequest(
        model="gpt-5",
        system=[],
        tools=[],
        messages=[],
        output_schema=schema,
    )
    payload = build_payload(req)
    assert "text" in payload
    assert payload["text"]["format"]["type"] == "json_schema"
    assert payload["text"]["format"]["name"] == "my_schema"
    assert payload["text"]["format"]["strict"] is True


# ── tool_choice payload ───────────────────────────────────────────────────────


def test_chat_payload_tool_choice_string():
    from agent_kit.providers.openai_chat import _build_chat_payload
    from agent_kit.types import ProviderRequest

    req = ProviderRequest(model="gpt-5", system=[], tools=[], messages=[], tool_choice="required")
    payload = _build_chat_payload(req)
    assert payload["tool_choice"] == "required"


def test_chat_payload_tool_choice_dict():
    from agent_kit.providers.openai_chat import _build_chat_payload
    from agent_kit.types import ProviderRequest

    req = ProviderRequest(
        model="gpt-5",
        system=[],
        tools=[],
        messages=[],
        tool_choice={"type": "tool", "name": "emit_sql"},
    )
    payload = _build_chat_payload(req)
    assert payload["tool_choice"]["type"] == "function"
    assert payload["tool_choice"]["function"]["name"] == "emit_sql"


def test_responses_payload_tool_choice():
    from agent_kit.openai_responses import build_payload
    from agent_kit.types import ProviderRequest

    req = ProviderRequest(
        model="gpt-5",
        system=[],
        tools=[],
        messages=[],
        tool_choice={"type": "tool", "name": "my_tool"},
    )
    payload = build_payload(req)
    assert payload["tool_choice"]["type"] == "function"
    assert payload["tool_choice"]["name"] == "my_tool"


# ── final_tool_name (terminal tool) ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_final_tool_terminates_loop_with_structured_output():
    from agent_kit import Agent
    from agent_kit.sessions import InMemorySessionStore
    from agent_kit.tools.registry import empty_tools

    tool_input = {"sql": "SELECT 1"}
    provider = _tool_use_provider("emit_answer", tool_input)

    agent = Agent(
        model="gpt-5",
        provider=provider,
        tools=empty_tools(_make_emit_tool()),
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        final_tool_name="emit_answer",
    )
    session = await agent.session()
    result = None
    tool_call_ends = []
    async for event in session.run("go"):
        if event.type == "result":
            result = event
        if event.type == "tool_call_end":
            tool_call_ends.append(event)

    assert result is not None
    assert result.structured_output == tool_input
    assert tool_call_ends == []


@pytest.mark.asyncio
async def test_final_tool_via_run_options():
    from agent_kit import Agent, RunOptions
    from agent_kit.sessions import InMemorySessionStore
    from agent_kit.tools.registry import empty_tools

    tool_input = {"sql": "SELECT 2"}
    provider = _tool_use_provider("emit_answer", tool_input)

    agent = Agent(
        model="gpt-5",
        provider=provider,
        tools=empty_tools(_make_emit_tool()),
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
    )
    session = await agent.session()
    result = None
    async for event in session.run("go", RunOptions(final_tool_name="emit_answer")):
        if event.type == "result":
            result = event

    assert result is not None
    assert result.structured_output == tool_input


# ── per-request effort ────────────────────────────────────────────────────────


def test_responses_per_request_effort_overrides_constructor():
    from agent_kit.openai_responses import OpenAIReasoning, build_payload
    from agent_kit.types import ProviderRequest

    constructor_reasoning = OpenAIReasoning(effort="low")
    req = ProviderRequest(
        model="gpt-5",
        system=[],
        tools=[],
        messages=[],
        effort="high",
    )
    payload = build_payload(req, reasoning=constructor_reasoning)
    assert payload["reasoning"]["effort"] == "high"


def test_responses_effort_from_reasoning_when_no_req_effort():
    from agent_kit.openai_responses import OpenAIReasoning, build_payload
    from agent_kit.types import ProviderRequest

    constructor_reasoning = OpenAIReasoning(effort="medium")
    req = ProviderRequest(model="gpt-5", system=[], tools=[], messages=[])
    payload = build_payload(req, reasoning=constructor_reasoning)
    assert payload["reasoning"]["effort"] == "medium"
