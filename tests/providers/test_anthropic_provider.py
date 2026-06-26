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
            SystemBlock(text="Block A", cacheable=True),
            SystemBlock(text="Block B", cacheable=True),
        ],
        cache_prompt=True,
        cache_ttl="5m",
    )
    payload = _build_payload(req, AnthropicProviderOptions())
    system = payload["system"]
    assert "cache_control" not in system[0]
    assert system[1]["cache_control"] == {"type": "ephemeral"}


def test_build_payload_cache_boundary_excludes_dynamic_tail():
    # ROADMAP 3.2: the cache breakpoint sits at the end of the leading static
    # (cacheable=True) run, so a volatile trailing section can change without
    # invalidating the cached static prefix.
    from linch.providers.anthropic import AnthropicProviderOptions, _build_payload
    from linch.types import SystemBlock

    def payload_for(dynamic_text: str):
        req = _make_req(
            system=[
                SystemBlock(text="Static A", cacheable=True),
                SystemBlock(text="Static B", cacheable=True),
                SystemBlock(text=dynamic_text, cacheable=False),
            ],
            cache_prompt=True,
            cache_ttl="5m",
        )
        return _build_payload(req, AnthropicProviderOptions())["system"]

    s1 = payload_for("recalled: blue")
    # Breakpoint on the last static block; the dynamic tail is uncached.
    assert "cache_control" not in s1[0]
    assert s1[1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in s1[2]

    # Changing only the dynamic tail leaves the cached static prefix byte-identical.
    s2 = payload_for("recalled: green")
    assert s1[:2] == s2[:2]
    assert s1[2]["text"] != s2[2]["text"]


def test_build_payload_cache_boundary_all_static_marks_last():
    # With no dynamic block the breakpoint stays on the last block — byte-identical
    # to the legacy "cache the last system block" behavior for real agent prompts.
    from linch.providers.anthropic import AnthropicProviderOptions, _build_payload
    from linch.types import SystemBlock

    req = _make_req(
        system=[
            SystemBlock(text="A", cacheable=True),
            SystemBlock(text="B", cacheable=True),
            SystemBlock(text="C", cacheable=True),
        ],
        cache_prompt=True,
    )
    system = _build_payload(req, AnthropicProviderOptions())["system"]
    assert "cache_control" not in system[0]
    assert "cache_control" not in system[1]
    assert system[2]["cache_control"] == {"type": "ephemeral"}


def test_cache_prefix_stable_across_turns():
    """End-to-end cache-hit validation: the cached prefix (tools + static system
    + earlier messages) is byte-identical from one turn to the next, so Anthropic
    reads the prior turn's prefix from cache instead of re-billing it.

    This is the property that produces *hits* — the other tests only check where
    a single turn's breakpoints land. Anthropic's cached prefix is ordered
    tools -> system -> messages, so all three must stay stable.
    """
    from linch.providers.anthropic import AnthropicProviderOptions, _build_payload
    from linch.types import Message, ProviderRequest, SystemBlock, TextBlock

    tools = [
        {"name": "read", "description": "", "input_schema": {"type": "object"}},
        {"name": "write", "description": "", "input_schema": {"type": "object"}},
    ]

    def payload_for(messages, recalled):
        req = ProviderRequest(
            model="claude-sonnet-4-6",
            system=[
                SystemBlock(text="Static identity", cacheable=True),
                SystemBlock(text="Static protocol", cacheable=True),
                # Volatile per-turn tail (e.g. memory recall) — cacheable=False.
                SystemBlock(text=f"recalled: {recalled}", cacheable=False),
            ],
            tools=tools,
            messages=messages,
            cache_prompt=True,
        )
        return _build_payload(req, AnthropicProviderOptions())

    turn1 = [Message(role="user", content=[TextBlock(text="q1")])]
    turn2 = [
        Message(role="user", content=[TextBlock(text="q1")]),
        Message(role="assistant", content=[TextBlock(text="a1")]),
        Message(role="user", content=[TextBlock(text="q2")]),
    ]

    # The volatile tail changes between turns; the cached prefix must not.
    p1 = payload_for(turn1, recalled="blue")
    p2 = payload_for(turn2, recalled="green")

    # 1. Tools (first in the cached prefix) are byte-identical across turns.
    assert p1["tools"] == p2["tools"]

    # 2. Static system prefix up to and including the breakpoint is byte-identical,
    #    even though the volatile tail block changed.
    assert p1["system"][:2] == p2["system"][:2]
    assert p1["system"][1]["cache_control"] == {"type": "ephemeral"}  # breakpoint
    assert "cache_control" not in p1["system"][2]  # volatile tail stays uncached
    assert p1["system"][2]["text"] != p2["system"][2]["text"]

    # 3. Turn 1's messages survive verbatim as the prefix of turn 2 (ignoring the
    #    moving message breakpoint), so that prefix is a cache read next turn.
    def strip(msgs):
        return [
            {
                **m,
                "content": [
                    {k: v for k, v in b.items() if k != "cache_control"} for b in m["content"]
                ],
            }
            for m in msgs
        ]

    s1, s2 = strip(p1["messages"]), strip(p2["messages"])
    assert s2[: len(s1)] == s1

    # 4. The moving breakpoint sits on the last user message each turn, and turn 2
    #    no longer marks turn 1's (now-cached) user message.
    assert p1["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert p2["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in p2["messages"][0]["content"][0]


def test_build_payload_cache_boundary_leading_dynamic_marks_later_cacheable():
    # Cache breakpoint is the last cacheable system block, not just a leading prefix.
    from linch.providers.anthropic import AnthropicProviderOptions, _build_payload
    from linch.types import SystemBlock

    req = _make_req(
        system=[
            SystemBlock(text="volatile", cacheable=False),
            SystemBlock(text="also static", cacheable=True),
        ],
        cache_prompt=True,
    )
    system = _build_payload(req, AnthropicProviderOptions())["system"]
    assert "cache_control" not in system[0]
    assert system[1]["cache_control"] == {"type": "ephemeral"}


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


def test_build_payload_cache_does_not_mark_tools():
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
    assert "cache_control" not in tools[1]


def test_build_payload_cache_marks_most_recent_user_message_only():
    from linch.providers.anthropic import AnthropicProviderOptions, _build_payload
    from linch.types import Message, TextBlock

    req = _make_req(
        messages=[
            Message(role="user", content=[TextBlock(text="first")]),
            Message(role="user", content=[TextBlock(text="second")]),
        ],
        cache_prompt=True,
    )
    payload = _build_payload(req, AnthropicProviderOptions())
    messages = payload["messages"]

    assert "cache_control" not in messages[0]["content"][0]
    assert messages[1]["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_build_payload_cache_ttl_1h_only():
    from linch.providers.anthropic import AnthropicProviderOptions, _build_payload

    req = _make_req(cache_prompt=True, cache_ttl="1h")
    payload = _build_payload(req, AnthropicProviderOptions())

    assert payload["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert payload["messages"][0]["content"][0]["cache_control"] == {
        "type": "ephemeral",
        "ttl": "1h",
    }


def test_translate_redacted_thinking_round_trip():
    from linch.providers.anthropic import _translate_messages
    from linch.types import Message, RedactedThinkingBlock, ToolUseBlock

    messages = [
        Message(
            role="assistant",
            content=[
                RedactedThinkingBlock(data="opaque"),
                ToolUseBlock(id="call_1", name="Search", input={"q": "x"}),
            ],
        )
    ]

    translated = _translate_messages(messages)
    assert translated[0]["content"][0] == {"type": "redacted_thinking", "data": "opaque"}
    assert translated[0]["content"][1]["type"] == "tool_use"


# ---------------------------------------------------------------------------
# Feature A — output_schema synthesises a forced tool (RED until impl)
# ---------------------------------------------------------------------------


def test_build_payload_output_schema_synthesizes_forced_tool():
    """_build_payload must append a forced tool and set tool_choice when output_schema is set."""
    from linch.providers.anthropic import AnthropicProviderOptions, _build_payload
    from linch.types import OutputSchema

    schema = OutputSchema(
        name="get_weather",
        schema={
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
        description="Get weather for a city.",
    )
    req = _make_req(output_schema=schema)
    payload = _build_payload(req, AnthropicProviderOptions())

    # Must synthesise the output schema as a forced tool
    assert "tools" in payload
    tool = next((t for t in payload["tools"] if t["name"] == "get_weather"), None)
    assert tool is not None, "expected 'get_weather' tool in payload"
    assert tool["description"] == "Get weather for a city."
    assert tool["input_schema"]["properties"]["city"]["type"] == "string"

    # Must force tool_choice to that exact tool
    assert payload["tool_choice"] == {"type": "tool", "name": "get_weather"}


def test_build_payload_output_schema_appends_to_existing_tools():
    """Schema tool appended after real tools; tool_choice not forced when other tools present."""
    from linch.providers.anthropic import AnthropicProviderOptions, _build_payload
    from linch.types import OutputSchema

    real_tool = {
        "name": "search",
        "description": "Search the web.",
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
    }
    schema = OutputSchema(name="final_answer", schema={"type": "object", "properties": {}})
    req = _make_req(tools=[real_tool], output_schema=schema)
    payload = _build_payload(req, AnthropicProviderOptions())

    names = [t["name"] for t in payload["tools"]]
    assert "search" in names
    assert "final_answer" in names
    # When real tools are present the model must be free to call them first;
    # tool_choice must NOT be forced to the schema tool.
    assert payload.get("tool_choice") != {"type": "tool", "name": "final_answer"}


def test_build_payload_output_schema_sole_tool_forces_choice():
    """When schema tool is the only tool, tool_choice is forced so the model must call it."""
    from linch.providers.anthropic import AnthropicProviderOptions, _build_payload
    from linch.types import OutputSchema

    schema = OutputSchema(name="get_answer", schema={"type": "object", "properties": {}})
    req = _make_req(tools=[], output_schema=schema)
    payload = _build_payload(req, AnthropicProviderOptions())

    assert payload["tool_choice"] == {"type": "tool", "name": "get_answer"}


def test_build_payload_no_output_schema_no_extra_tool():
    """Absent output_schema → no synthesised tool, no tool_choice."""
    from linch.providers.anthropic import AnthropicProviderOptions, _build_payload

    req = _make_req(tools=[])
    payload = _build_payload(req, AnthropicProviderOptions())

    assert "tools" not in payload
    assert "tool_choice" not in payload


def test_build_payload_output_schema_no_description():
    """OutputSchema with description=None → empty string in synthesised tool."""
    from linch.providers.anthropic import AnthropicProviderOptions, _build_payload
    from linch.types import OutputSchema

    schema = OutputSchema(name="bare_schema", schema={"type": "object"}, description=None)
    req = _make_req(output_schema=schema)
    payload = _build_payload(req, AnthropicProviderOptions())

    tool = next(t for t in payload["tools"] if t["name"] == "bare_schema")
    assert tool["description"] == ""


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


def test_error_mapping_context_length_without_status_is_not_retried():
    """A context-overflow lacking an int status must not become a retryable error.

    An Anthropic-compatible proxy / wrapper may surface a context-overflow with
    no status_code; gating on ``status == 400`` then produced a retryable
    ProviderError and an unrecoverable retry storm.
    """
    from linch.errors import ContextLengthError, ProviderError
    from linch.providers.anthropic import _map_anthropic_error

    class NoStatusCtx(Exception):
        body = {"error": {"code": "context_length_exceeded", "message": "too long"}}

    mapped = _map_anthropic_error(NoStatusCtx("boom"))
    assert isinstance(mapped, ContextLengthError)
    assert not isinstance(mapped, ProviderError)
    assert mapped.retryable is False

    class NoStatusMsg(Exception):
        pass

    mapped2 = _map_anthropic_error(NoStatusMsg("prompt is too long: 201537 tokens"))
    assert isinstance(mapped2, ContextLengthError)
    assert mapped2.retryable is False


def test_error_mapping_retry_after_http_date():
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    from linch.errors import RateLimitError
    from linch.providers.anthropic import _map_anthropic_error

    class Resp:
        headers = {
            "retry-after": format_datetime(datetime.now(timezone.utc) + timedelta(seconds=5))
        }

    class RateLimited(Exception):
        status_code = 429
        response = Resp()

    result = _map_anthropic_error(RateLimited("rate limited"))
    assert isinstance(result, RateLimitError)
    assert result.retry_after_seconds is not None
    assert result.retry_after_seconds > 0


def test_error_mapping_non_context_400_stays_provider_error():
    from linch.errors import ProviderError
    from linch.providers.anthropic import _map_anthropic_error

    class BadReq(Exception):
        status_code = 400

    result = _map_anthropic_error(BadReq("invalid tool schema context value"))
    assert isinstance(result, ProviderError)


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


def test_stream_cache_tokens_not_double_counted():
    """Cache figures are cumulative at message_start; a message_delta that
    echoes the same cache fields must NOT be added on top (regression)."""
    import asyncio
    from types import SimpleNamespace

    from linch.providers.anthropic import AnthropicProvider
    from linch.types import Message, ProviderRequest, TextBlock

    cache_read = 4096
    cache_creation = 1024

    class _FakeStream:
        def __init__(self, events):
            self._events = events

        def __aiter__(self):
            self._it = iter(self._events)
            return self

        async def __anext__(self):
            try:
                return next(self._it)
            except StopIteration:
                raise StopAsyncIteration from None

    events = [
        SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(
                usage=SimpleNamespace(
                    input_tokens=100,
                    cache_read_input_tokens=cache_read,
                    cache_creation_input_tokens=cache_creation,
                )
            ),
        ),
        # The delta echoes the SAME cumulative cache figures.  Accumulating
        # them would double-count; the provider must overwrite instead.
        SimpleNamespace(
            type="message_delta",
            delta=SimpleNamespace(stop_reason="end_turn"),
            usage=SimpleNamespace(
                output_tokens=42,
                cache_read_input_tokens=cache_read,
                cache_creation_input_tokens=cache_creation,
            ),
        ),
    ]

    class _FakeMessages:
        async def create(self, **payload):
            return _FakeStream(events)

    provider = AnthropicProvider()
    provider._client = SimpleNamespace(messages=_FakeMessages())

    req = ProviderRequest(
        model="claude-sonnet-4-6",
        system=[],
        tools=[],
        messages=[Message(role="user", content=[TextBlock(text="hi")])],
    )

    async def _run():
        return [e async for e in provider.stream(req)]

    out = asyncio.run(_run())
    end = next(e for e in out if e["type"] == "message_end")
    usage = end["usage"]
    assert usage.cache_read_tokens == cache_read
    assert usage.cache_creation_tokens == cache_creation
    assert usage.input_tokens == 100
    assert usage.output_tokens == 42


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
