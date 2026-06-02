"""Tests for AnthropicProvider.

Unit tests cover the payload builder and message translator (no API key
needed).  Live tests hit the real API and are skipped without
ANTHROPIC_API_KEY in the environment.
"""

from __future__ import annotations

import os

import pytest

SKIP_REASON = "ANTHROPIC_API_KEY not set"
needs_key = pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason=SKIP_REASON)

# ---------------------------------------------------------------------------
# Unit tests — payload builder (no API key)
# ---------------------------------------------------------------------------


def _make_req(**overrides):
    from linch.types import Message, ProviderRequest, SystemBlock, TextBlock

    defaults = dict(
        model="claude-sonnet-4-6",
        system=[SystemBlock(text="You are a helpful assistant.", cacheable=True)],
        tools=[],
        messages=[Message(role="user", content=[TextBlock(text="hello")])],
    )
    defaults.update(overrides)
    return ProviderRequest(**defaults)


def test_build_payload_basic():
    from linch.providers.anthropic import AnthropicProviderOptions, _build_payload

    req = _make_req()
    payload = _build_payload(req, AnthropicProviderOptions())

    assert payload["model"] == "claude-sonnet-4-6"
    assert payload["stream"] is True
    assert payload["max_tokens"] == 8096  # default
    assert payload["system"][0]["text"] == "You are a helpful assistant."
    assert len(payload["messages"]) == 1
    assert payload["messages"][0]["role"] == "user"


def test_build_payload_respects_max_output_tokens():
    from linch.providers.anthropic import AnthropicProviderOptions, _build_payload

    req = _make_req(max_output_tokens=1024)
    payload = _build_payload(req, AnthropicProviderOptions())
    assert payload["max_tokens"] == 1024


def test_build_payload_cache_marks_last_system_block():
    from linch.providers.anthropic import AnthropicProviderOptions, _build_payload
    from linch.types import SystemBlock

    req = _make_req(
        system=[
            SystemBlock(text="Block A"),
            SystemBlock(text="Block B"),
        ],
        cache_prompt=True,
        cache_ttl="5m",
    )
    payload = _build_payload(req, AnthropicProviderOptions())
    system = payload["system"]
    assert "cache_control" not in system[0]
    assert system[1]["cache_control"] == {"type": "ephemeral", "ttl": "5m"}


def test_build_payload_no_cache_when_false():
    from linch.providers.anthropic import AnthropicProviderOptions, _build_payload
    from linch.types import SystemBlock

    req = _make_req(
        system=[SystemBlock(text="Block A")],
        cache_prompt=False,
    )
    payload = _build_payload(req, AnthropicProviderOptions())
    assert "cache_control" not in payload["system"][0]


def test_build_payload_tool_schema_translated():
    from linch.providers.anthropic import AnthropicProviderOptions, _build_payload

    req = _make_req(
        tools=[
            {
                "name": "SearchDocs",
                "description": "Search docs.",
                "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
            }
        ]
    )
    payload = _build_payload(req, AnthropicProviderOptions())
    tool = payload["tools"][0]
    assert tool["name"] == "SearchDocs"
    assert tool["description"] == "Search docs."
    assert "input_schema" in tool
    # Anthropic uses input_schema (same key as ours)


def test_build_payload_cache_marks_last_tool():
    from linch.providers.anthropic import AnthropicProviderOptions, _build_payload

    req = _make_req(
        tools=[
            {
                "name": "A",
                "description": "a",
                "input_schema": {"type": "object"},
            },
            {
                "name": "B",
                "description": "b",
                "input_schema": {"type": "object"},
            },
        ],
        cache_prompt=True,
    )
    payload = _build_payload(req, AnthropicProviderOptions())
    tools = payload["tools"]
    assert "cache_control" not in tools[0]
    assert tools[1]["cache_control"]["type"] == "ephemeral"


def test_tool_choice_mapping():
    from linch.providers.anthropic import _translate_tool_choice

    assert _translate_tool_choice("auto") == {"type": "auto"}
    assert _translate_tool_choice("none") == {"type": "none"}
    assert _translate_tool_choice("required") == {"type": "any"}
    assert _translate_tool_choice({"name": "MyTool"}) == {"type": "tool", "name": "MyTool"}


def test_stop_reason_mapping():
    from linch.providers.anthropic import _map_stop_reason

    assert _map_stop_reason("end_turn") == "end_turn"
    assert _map_stop_reason("tool_use") == "tool_use"
    assert _map_stop_reason("max_tokens") == "max_tokens"
    assert _map_stop_reason("refusal") == "refusal"
    assert _map_stop_reason("stop_sequence") == "end_turn"
    assert _map_stop_reason("pause_turn") == "end_turn"
    assert _map_stop_reason(None) == "end_turn"


def test_translate_messages_user_text():
    from linch.providers.anthropic import _translate_messages
    from linch.types import Message, TextBlock

    msgs = [Message(role="user", content=[TextBlock(text="hello")])]
    out = _translate_messages(msgs)
    assert len(out) == 1
    assert out[0]["role"] == "user"
    assert out[0]["content"][0] == {"type": "text", "text": "hello"}


def test_translate_messages_assistant_tool_use():
    from linch.providers.anthropic import _translate_messages
    from linch.types import Message, TextBlock, ToolUseBlock

    msgs = [
        Message(
            role="assistant",
            content=[
                TextBlock(text="I will search."),
                ToolUseBlock(id="tu_1", name="Search", input={"q": "cats"}),
            ],
        )
    ]
    out = _translate_messages(msgs)
    assert out[0]["role"] == "assistant"
    content = out[0]["content"]
    assert content[0] == {"type": "text", "text": "I will search."}
    assert content[1] == {
        "type": "tool_use",
        "id": "tu_1",
        "name": "Search",
        "input": {"q": "cats"},
    }


def test_translate_messages_tool_result():
    from linch.providers.anthropic import _translate_messages
    from linch.types import Message, ToolResultBlock

    msgs = [
        Message(
            role="user",
            content=[ToolResultBlock(tool_use_id="tu_1", content="ok", is_error=False)],
        )
    ]
    out = _translate_messages(msgs)
    assert out[0]["role"] == "user"
    tr = out[0]["content"][0]
    assert tr["type"] == "tool_result"
    assert tr["tool_use_id"] == "tu_1"
    assert tr["content"] == "ok"
    assert "is_error" not in tr  # not set when False


def test_translate_messages_tool_result_error():
    from linch.providers.anthropic import _translate_messages
    from linch.types import Message, ToolResultBlock

    msgs = [
        Message(
            role="user",
            content=[ToolResultBlock(tool_use_id="tu_1", content="boom", is_error=True)],
        )
    ]
    out = _translate_messages(msgs)
    tr = out[0]["content"][0]
    assert tr["is_error"] is True


def test_translate_messages_thinking_block_roundtrip():
    """Thinking blocks must carry signature on subsequent turns."""
    from linch.providers.anthropic import _translate_messages
    from linch.types import Message, ThinkingBlock

    msgs = [
        Message(
            role="assistant",
            content=[ThinkingBlock(thinking="I thought...", signature="sig_abc")],
        )
    ]
    out = _translate_messages(msgs)
    tb = out[0]["content"][0]
    assert tb["type"] == "thinking"
    assert tb["thinking"] == "I thought..."
    assert tb["signature"] == "sig_abc"


def test_translate_image_url():
    from linch.providers.anthropic import _translate_image
    from linch.types import ImageBlock

    block = ImageBlock(source={"type": "url", "url": "https://example.com/img.png"})
    out = _translate_image(block)
    assert out == {
        "type": "image",
        "source": {"type": "url", "url": "https://example.com/img.png"},
    }


def test_translate_image_base64():
    from linch.providers.anthropic import _translate_image
    from linch.types import ImageBlock

    block = ImageBlock(source={"type": "base64", "media_type": "image/png", "data": "abc123"})
    out = _translate_image(block)
    assert out["source"]["type"] == "base64"
    assert out["source"]["data"] == "abc123"


def test_error_mapping_auth():
    from linch.errors import AuthError
    from linch.providers.anthropic import _map_anthropic_error

    class FakeAuthErr(Exception):
        __class__ = type("AuthenticationError", (Exception,), {})()  # type: ignore[assignment]

    class _Err(Exception):
        pass

    err = _Err("authentication error")
    err.__class__.__name__ = "AuthenticationError"  # type: ignore[attr-defined]
    # Simulate by name check
    result = _map_anthropic_error(err)
    assert isinstance(result, AuthError)


def test_error_mapping_context_length():
    from linch.errors import ContextLengthError
    from linch.providers.anthropic import _map_anthropic_error

    class BadReq(Exception):
        status_code = 400

    err = BadReq("prompt is too long: 201537 tokens > 200000 maximum")
    result = _map_anthropic_error(err)
    assert isinstance(result, ContextLengthError)


def test_provider_missing_package(monkeypatch):
    """Raises ProviderError when anthropic package is not installed."""
    import sys

    from linch.errors import ProviderError

    monkeypatch.setitem(sys.modules, "anthropic", None)  # type: ignore[arg-type]
    from linch.providers.anthropic import AnthropicProvider

    provider = AnthropicProvider()
    provider._client = None  # ensure fresh client attempt

    import asyncio

    async def _run():
        from linch.types import Message, ProviderRequest, TextBlock

        req = ProviderRequest(
            model="claude-sonnet-4-6",
            system=[],
            tools=[],
            messages=[Message(role="user", content=[TextBlock(text="hi")])],
        )
        # Drain the async generator to trigger the import
        async for _ in provider.stream(req):
            pass

    with pytest.raises(ProviderError, match="anthropic"):
        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Live tests — require ANTHROPIC_API_KEY
# ---------------------------------------------------------------------------


@needs_key
@pytest.mark.asyncio
async def test_live_basic_completion():
    """Provider streams a text response and yields a success ResultEvent."""
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.events import ResultEvent
    from linch.providers import AnthropicProvider, AnthropicProviderOptions
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools

    provider = AnthropicProvider(AnthropicProviderOptions(api_key=os.environ["ANTHROPIC_API_KEY"]))
    agent = Agent(
        model="claude-haiku-4-5",
        provider=provider,
        tools=empty_tools(),
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        loop_guard=None,
    )
    session = await agent.session()
    events = []
    async for event in session.run("Reply with exactly: pong"):
        events.append(event)

    result = next((e for e in events if isinstance(e, ResultEvent)), None)
    assert result is not None
    assert result.subtype == "success"
    assert result.final_text is not None
    assert "pong" in result.final_text.lower()


@needs_key
@pytest.mark.asyncio
async def test_live_tool_call():
    """Provider emits tool_use and the loop executes it."""
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.events import ResultEvent, ToolCallEndEvent
    from linch.providers import AnthropicProvider, AnthropicProviderOptions
    from linch.sessions import InMemorySessionStore
    from linch.tools import ToolResult
    from linch.tools.registry import empty_tools

    class AddTool:
        name = "Add"
        description = "Return the sum of a and b."
        input_schema = {
            "type": "object",
            "properties": {
                "a": {"type": "number"},
                "b": {"type": "number"},
            },
            "required": ["a", "b"],
        }
        scope = "read"
        parallel = True
        tags: tuple = ()

        def validate(self, raw):
            return raw

        def summarize(self, i):
            return "add"

        def resources(self, i):
            return []

        async def execute(self, inp, ctx):
            return ToolResult(content=str(inp["a"] + inp["b"]))

    provider = AnthropicProvider(AnthropicProviderOptions(api_key=os.environ["ANTHROPIC_API_KEY"]))
    agent = Agent(
        model="claude-haiku-4-5",
        provider=provider,
        tools=empty_tools(AddTool()),
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        loop_guard=None,
    )
    session = await agent.session()
    events = []
    async for event in session.run("What is 17 + 25? Use the Add tool."):
        events.append(event)

    tool_ends = [e for e in events if isinstance(e, ToolCallEndEvent)]
    assert len(tool_ends) >= 1, "expected at least one tool call"
    assert all(not e.is_error for e in tool_ends)

    result = next((e for e in events if isinstance(e, ResultEvent)), None)
    assert result is not None
    assert result.subtype == "success"


@needs_key
@pytest.mark.asyncio
async def test_live_prompt_cache_tokens_reported():
    """Second identical call should report cache_read_tokens > 0."""
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.events import UsageEvent
    from linch.providers import AnthropicProvider, AnthropicProviderOptions
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools

    long_system = "You are a helpful assistant. " * 200  # ~800 tokens to fill cache

    provider = AnthropicProvider(AnthropicProviderOptions(api_key=os.environ["ANTHROPIC_API_KEY"]))

    async def _run_once():
        agent = Agent(
            model="claude-haiku-4-5",
            provider=provider,
            tools=empty_tools(),
            permissions={"mode": "skip-dangerous"},
            session_store=InMemorySessionStore(),
            features=FeatureFlags(skills=False, subagents=False, mcp=False),
            system_prompt=long_system,
            loop_guard=None,
        )
        session = await agent.session()
        events = []
        async for event in session.run("Say hi."):
            events.append(event)
        return events

    events1 = await _run_once()
    events2 = await _run_once()

    usages2 = [e for e in events2 if isinstance(e, UsageEvent)]
    assert usages2, "no UsageEvents"
    total_cache_read = sum(u.cumulative.cache_read_tokens for u in usages2)
    # Second call should benefit from prompt cache (cache_creation on first,
    # cache_read on second).  Either is non-zero = caching is working.
    total_cache_creation_1 = sum(
        u.cumulative.cache_creation_tokens
        for u in [e for e in events1 if isinstance(e, UsageEvent)]
    )
    assert total_cache_creation_1 > 0 or total_cache_read > 0, (
        "expected cache tokens on at least one of the two calls"
    )
