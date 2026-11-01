"""Tests for structured output (OutputSchema + tool_choice + final_tool_name).

NOTE: linch imports inside test functions (not module-level) so tests are
robust to test_hardening.py's sys.modules reset.
"""

from __future__ import annotations

import json

import pytest

from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn, ToolUseTurn
from tests.support.tools import StubTool, tool_registry

# ── Provider helpers (lazy imports inside) ───────────────────────────────────


def _text_provider(text: str):
    """Return a scripted text provider with native schema output disabled."""

    from linch.providers import ProviderCapabilities

    class TextProvider(ScriptedProvider):
        def capabilities(self, model: str) -> ProviderCapabilities:
            return ProviderCapabilities(context_window=self.context_window(model))

    return TextProvider([TextTurn(text)])


def _tool_use_provider(tool_name: str, tool_input: dict):
    return ScriptedProvider(
        [ToolUseTurn(tool_name=tool_name, tool_input=tool_input, tool_id="t1"), TextTurn("done")]
    )


def _make_emit_tool():
    return StubTool(name="emit_answer", output="should not run")


def _request(**overrides):
    from linch.types import ProviderRequest

    values = {"model": "gpt-5", "system": [], "tools": [], "messages": []}
    values.update(overrides)
    return ProviderRequest(**values)


# ── OutputSchema ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "prefix,suffix",
    [("", ""), ("```json\n", "\n```"), ("**Here you go:**\n\n```json\n", "\n```")],
)
@pytest.mark.asyncio
async def test_structured_output_from_json_text(prefix: str, suffix: str):
    from linch.types import OutputSchema

    payload = {"answer": "42", "confidence": 0.9}
    provider = _text_provider(f"{prefix}{json.dumps(payload)}{suffix}")

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
    agent = make_agent(provider, output_schema=schema)
    session = await agent.session()
    result = None
    async for event in session.run("go"):
        if event.type == "result":
            result = event

    assert result is not None
    assert result.structured_output == payload
    assert result.structured_error is None


@pytest.mark.asyncio
async def test_empty_text_with_output_schema_reports_parse_error():
    from linch.types import OutputSchema

    provider = _text_provider("")
    schema = OutputSchema(name="answer", schema={"type": "object"})
    agent = make_agent(provider, reliability=None, output_schema=schema)
    session = await agent.session()
    result = [event async for event in session.run("go")][-1]

    assert result.subtype == "success"
    assert result.structured_output is None
    assert "JSON parse error" in result.structured_error


@pytest.mark.asyncio
async def test_structured_output_malformed_json():
    from linch.types import OutputSchema

    provider = _text_provider("not valid json {{")

    schema = OutputSchema(name="s", schema={"type": "object"})
    agent = make_agent(provider, output_schema=schema)
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
async def test_structured_output_literal_null():
    """A literal JSON `null` response must be reported as a schema error, not silently
    accepted — `null` parses successfully to Python None, which must not be confused
    with the "raw text failed to parse" sentinel."""
    from linch.types import OutputSchema

    provider = _text_provider("null")

    schema = OutputSchema(name="s", schema={"type": "object"})
    agent = make_agent(provider, output_schema=schema)
    session = await agent.session()
    result = None
    async for event in session.run("go"):
        if event.type == "result":
            result = event

    assert result is not None
    assert result.structured_output is None
    assert result.structured_error is not None
    assert "NoneType" in result.structured_error


@pytest.mark.asyncio
async def test_no_output_schema_structured_output_is_none():
    provider = _text_provider('{"key": "value"}')

    agent = make_agent(provider)
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
    from linch.providers.openai_chat import _build_chat_payload
    from linch.types import OutputSchema

    schema = OutputSchema(name="my_schema", schema={"type": "object"})
    req = _request(output_schema=schema)
    payload = _build_chat_payload(req)
    assert "response_format" in payload
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["response_format"]["json_schema"]["name"] == "my_schema"


def test_chat_payload_no_schema_no_response_format():
    from linch.providers.openai_chat import _build_chat_payload

    req = _request()
    payload = _build_chat_payload(req)
    assert "response_format" not in payload


# ── Responses provider payload ────────────────────────────────────────────────


def test_responses_payload_text_format():
    from linch.openai_responses import build_payload
    from linch.types import OutputSchema

    schema = OutputSchema(name="my_schema", schema={"type": "object"}, strict=True)
    req = _request(output_schema=schema)
    payload = build_payload(req)
    assert "text" in payload
    assert payload["text"]["format"]["type"] == "json_schema"
    assert payload["text"]["format"]["name"] == "my_schema"
    assert payload["text"]["format"]["strict"] is True


# ── tool_choice payload ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "choice,expected",
    [
        ("required", "required"),
        ({"type": "tool", "name": "emit_sql"}, {"type": "function", "name": "emit_sql"}),
    ],
)
def test_chat_payload_tool_choice(choice, expected):
    from linch.providers.openai_chat import _build_chat_payload

    req = _request(tool_choice=choice)
    payload = _build_chat_payload(req)
    if isinstance(expected, str):
        assert payload["tool_choice"] == expected
    else:
        assert payload["tool_choice"]["type"] == expected["type"]
        assert payload["tool_choice"]["function"]["name"] == expected["name"]


def test_responses_payload_tool_choice():
    from linch.openai_responses import build_payload

    req = _request(tool_choice={"type": "tool", "name": "my_tool"})
    payload = build_payload(req)
    assert payload["tool_choice"]["type"] == "function"
    assert payload["tool_choice"]["name"] == "my_tool"


# ── final_tool_name (terminal tool) ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_final_tool_terminates_loop_with_structured_output():
    tool_input = {"sql": "SELECT 1"}
    provider = _tool_use_provider("emit_answer", tool_input)

    agent = make_agent(
        provider, tools=tool_registry(_make_emit_tool()), final_tool_name="emit_answer"
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
    # The synthetic terminal tool is not scheduled, but its protocol bracket
    # is durably closed so this session can be used for another provider turn.
    from linch.types import ToolResultBlock

    assert isinstance(session.provider_view[-1].content[0], ToolResultBlock)
    assert session.provider_view[-1].content[0].tool_use_id == "t1"
    follow_up = [event async for event in session.run("continue")]
    assert follow_up[-1].type == "result" and follow_up[-1].subtype == "success"


@pytest.mark.asyncio
async def test_final_tool_mixed_with_ordinary_call_fails_without_executing_either():
    from linch.types import Usage

    executions = 0

    def record_execution(input, ctx):
        nonlocal executions
        executions += 1

    ordinary = StubTool(name="ordinary", on_execute=record_execution)

    class Provider:
        id = "mixed-final"

        def context_window(self, model):
            return 128_000

        async def stream(self, req):
            yield {"type": "message_start", "model": req.model}
            for tool_id, name in (("ordinary-1", "ordinary"), ("final-1", "emit_answer")):
                yield {"type": "tool_use_start", "id": tool_id, "name": name}
                yield {"type": "tool_use_input_delta", "id": tool_id, "json_delta": "{}"}
                yield {"type": "tool_use_end", "id": tool_id}
            yield {"type": "message_end", "stop_reason": "tool_use", "usage": Usage()}

    agent = make_agent(
        Provider(),
        tools=tool_registry(ordinary, _make_emit_tool()),
        final_tool_name="emit_answer",
    )
    session = await agent.session()
    events = [event async for event in session.run("go")]

    assert executions == 0
    assert any(event.type == "error" for event in events)
    assert events[-1].type == "result" and events[-1].subtype == "error"
    assert len(session.provider_view[-1].content) == 2


@pytest.mark.asyncio
async def test_final_tool_via_run_options():
    from linch import RunOptions

    tool_input = {"sql": "SELECT 2"}
    provider = _tool_use_provider("emit_answer", tool_input)

    agent = make_agent(provider, tools=tool_registry(_make_emit_tool()))
    session = await agent.session()
    result = None
    async for event in session.run("go", RunOptions(final_tool_name="emit_answer")):
        if event.type == "result":
            result = event

    assert result is not None
    assert result.structured_output == tool_input


# ── per-request effort ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "constructor_effort,request_effort,expected",
    [("low", "high", "high"), ("medium", None, "medium")],
)
def test_responses_effort_resolution(
    constructor_effort: str, request_effort: str | None, expected: str
):
    from linch.openai_responses import OpenAIReasoning, build_payload

    constructor_reasoning = OpenAIReasoning(effort=constructor_effort)
    req = _request(**({"effort": request_effort} if request_effort is not None else {}))
    payload = build_payload(req, reasoning=constructor_reasoning)
    assert payload["reasoning"]["effort"] == expected


# ── Terminal-tool structured output ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_terminal_tool_structured_output_via_forced_tool():
    """Loop captures forced-tool response as structured_output without executing the tool.

    This provider mimics an Anthropic-compatible fallback: it declares a
    terminal structured-output tool and returns a tool_use block named after
    the output schema. The loop must route final_block.input into
    structured_output via the terminal-tool path, NOT attempt to execute the
    tool and NOT emit a tool_call_end event.
    """
    from linch.providers import ProviderCapabilities
    from linch.types import OutputSchema

    payload = {"answer": "Paris", "confidence": 0.95}
    schema_name = "structured_response"

    class TerminalProvider(ScriptedProvider):
        def capabilities(self, model: str) -> ProviderCapabilities:
            return ProviderCapabilities(
                context_window=self.context_window(model),
                structured_output=True,
                structured_output_terminal_tool=True,
                tool_choice=True,
                prompt_cache=True,
            )

    provider = TerminalProvider([ToolUseTurn(schema_name, payload, tool_id="t1")])
    schema = OutputSchema(
        name=schema_name,
        schema={
            "type": "object",
            "properties": {
                "answer": {"type": "string"},
                "confidence": {"type": "number"},
            },
        },
    )
    agent = make_agent(provider, model="claude-sonnet-4-6", output_schema=schema)
    session = await agent.session()

    events = []
    async for event in session.run("What is the capital of France?"):
        events.append(event)

    result = next((e for e in events if e.type == "result"), None)
    assert result is not None
    assert result.structured_output == payload
    assert result.structured_error is None

    # The schema tool MUST NOT have been executed as a real tool
    tool_call_end_events = [e for e in events if e.type == "tool_call_end"]
    assert tool_call_end_events == [], "schema tool must not be executed as a real tool"
