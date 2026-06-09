"""Tests for Feature C — GeminiProvider (RED until implemented).

Tests do NOT require google-generativeai to be installed:
- Capability declarations are tested without making API calls.
- Payload construction is tested via an internal method.
- Full streaming uses a monkey-patched/fake google SDK object.
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Unit: import + instantiation
# ---------------------------------------------------------------------------


def test_gemini_provider_importable():
    """GeminiProvider is importable from linch.providers.gemini."""
    from linch.providers.gemini import GeminiProvider

    assert GeminiProvider is not None


def test_gemini_provider_options_importable():
    """GeminiProviderOptions is importable."""
    from linch.providers.gemini import GeminiProviderOptions

    assert GeminiProviderOptions is not None


def test_gemini_provider_instantiation():
    """GeminiProvider can be instantiated with no arguments."""
    from linch.providers.gemini import GeminiProvider

    provider = GeminiProvider()
    assert provider.id == "gemini"


def test_gemini_provider_options_defaults():
    """GeminiProviderOptions has api_key=None and project=None by default."""
    from linch.providers.gemini import GeminiProviderOptions

    opts = GeminiProviderOptions()
    assert opts.api_key is None


# ---------------------------------------------------------------------------
# Unit: context_window
# ---------------------------------------------------------------------------


def test_gemini_context_window_flash():
    """gemini-2.0-flash context window is at least 1 000 000."""
    from linch.providers.gemini import GeminiProvider

    provider = GeminiProvider()
    assert provider.context_window("gemini-2.0-flash") >= 1_000_000


def test_gemini_context_window_pro():
    """gemini-2.5-pro context window is at least 1 000 000."""
    from linch.providers.gemini import GeminiProvider

    provider = GeminiProvider()
    assert provider.context_window("gemini-2.5-pro") >= 1_000_000


def test_gemini_context_window_15_pro():
    """gemini-1.5-pro context window matches Gemini's documented limit."""
    from linch.providers.gemini import GeminiProvider

    provider = GeminiProvider()
    assert provider.context_window("gemini-1.5-pro") == 2_000_000


def test_gemini_context_window_unknown_defaults():
    """Unknown model IDs fall back to a reasonable default (≥ 32 000)."""
    from linch.providers.gemini import GeminiProvider

    provider = GeminiProvider()
    assert provider.context_window("gemini-unknown-xyz") >= 32_000


# ---------------------------------------------------------------------------
# Unit: capabilities
# ---------------------------------------------------------------------------


def test_gemini_capabilities_structured_output():
    """GeminiProvider declares structured_output=True."""
    from linch.providers.gemini import GeminiProvider

    caps = GeminiProvider().capabilities("gemini-2.5-pro")
    assert caps.structured_output is True


def test_gemini_capabilities_tool_choice():
    """GeminiProvider declares tool_choice=True."""
    from linch.providers.gemini import GeminiProvider

    caps = GeminiProvider().capabilities("gemini-2.5-pro")
    assert caps.tool_choice is True


def test_gemini_capabilities_no_prompt_cache():
    """GeminiProvider declares prompt_cache=False (not GA on Vertex)."""
    from linch.providers.gemini import GeminiProvider

    caps = GeminiProvider().capabilities("gemini-2.5-pro")
    assert caps.prompt_cache is False


# ---------------------------------------------------------------------------
# Unit: payload translation helpers
# ---------------------------------------------------------------------------


def test_translate_messages_text_only():
    """_translate_messages converts a user TextBlock to Gemini content format."""
    from linch.providers.gemini import _translate_messages
    from linch.types import Message, TextBlock

    messages = [Message(role="user", content=[TextBlock(text="hello")])]
    contents = _translate_messages(messages)
    assert len(contents) == 1
    assert contents[0]["role"] == "user"
    parts = contents[0]["parts"]
    assert any(p.get("text") == "hello" for p in parts)


def test_translate_messages_assistant():
    """_translate_messages converts assistant TextBlock with role='model'."""
    from linch.providers.gemini import _translate_messages
    from linch.types import Message, TextBlock

    messages = [Message(role="assistant", content=[TextBlock(text="I'll help.")])]
    contents = _translate_messages(messages)
    assert contents[0]["role"] == "model"


def test_translate_messages_tool_result_uses_tool_name():
    """Tool results are translated with Gemini's function name, not call id."""
    from linch.providers.gemini import _translate_messages
    from linch.types import Message, ToolResultBlock, ToolUseBlock

    messages = [
        Message(
            role="assistant",
            content=[ToolUseBlock(id="call-1", name="Read", input={"file_path": "README.md"})],
        ),
        Message(
            role="user",
            content=[ToolResultBlock(tool_use_id="call-1", content="file contents")],
        ),
    ]

    contents = _translate_messages(messages)
    function_response = contents[1]["parts"][0]["function_response"]
    assert function_response["id"] == "call-1"
    assert function_response["name"] == "Read"
    assert function_response["response"] == {"content": "file contents", "is_error": False}


def test_translate_tools():
    """_translate_tools produces Gemini-format function declarations."""
    from linch.providers.gemini import _translate_tools

    tools = [
        {
            "name": "Read",
            "description": "Read a file.",
            "input_schema": {
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
                "required": ["file_path"],
            },
        }
    ]
    gemini_tools = _translate_tools(tools)
    assert len(gemini_tools) == 1
    assert gemini_tools[0]["name"] == "Read"
    assert "parameters" in gemini_tools[0]


# ---------------------------------------------------------------------------
# Integration: stream() with a fake google SDK (no network)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_text_response():
    """GeminiProvider.stream() yields text_delta + message_end events."""
    import sys
    from unittest.mock import MagicMock, patch

    from linch.providers.gemini import GeminiProvider
    from linch.types import Message, ProviderRequest, TextBlock

    # Build a minimal fake google.generativeai module
    fake_part = MagicMock()
    fake_part.text = "Paris is the capital."
    fake_part.function_call = None

    fake_candidate = MagicMock()
    fake_candidate.content.parts = [fake_part]
    fake_candidate.finish_reason = 1  # STOP

    fake_chunk = MagicMock()
    fake_chunk.candidates = [fake_candidate]
    fake_chunk.usage_metadata.prompt_token_count = 10
    fake_chunk.usage_metadata.candidates_token_count = 5

    async def _fake_stream(*args, **kwargs):
        yield fake_chunk

    fake_model = MagicMock()
    fake_model.generate_content_async = MagicMock(return_value=_fake_stream())

    fake_genai = MagicMock()
    fake_genai.GenerativeModel.return_value = fake_model

    with patch.dict(sys.modules, {"google.generativeai": fake_genai}):
        provider = GeminiProvider()
        req = ProviderRequest(
            model="gemini-2.0-flash",
            messages=[Message(role="user", content=[TextBlock(text="capital of France?")])],
            system=[],
            tools=[],
            max_output_tokens=512,
        )
        events = [e async for e in provider.stream(req)]

    types = [e["type"] for e in events]
    assert "text_delta" in types
    assert "message_end" in types

    text_events = [e for e in events if e["type"] == "text_delta"]
    assert any("Paris" in e.get("text", "") for e in text_events)


@pytest.mark.asyncio
async def test_stream_tool_use():
    """GeminiProvider.stream() maps function_call to tool_use_* events."""
    import sys
    from unittest.mock import MagicMock, patch

    from linch.providers.gemini import GeminiProvider
    from linch.types import Message, ProviderRequest, TextBlock

    fake_fc = MagicMock()
    fake_fc.name = "Read"
    fake_fc.args = {"file_path": "README.md"}

    fake_part = MagicMock()
    fake_part.text = ""
    fake_part.function_call = fake_fc

    fake_candidate = MagicMock()
    fake_candidate.content.parts = [fake_part]
    fake_candidate.finish_reason = 2  # TOOL_USE / FUNCTION_CALL (Gemini uses 2)

    fake_chunk = MagicMock()
    fake_chunk.candidates = [fake_candidate]
    fake_chunk.usage_metadata.prompt_token_count = 8
    fake_chunk.usage_metadata.candidates_token_count = 3

    async def _fake_stream(*args, **kwargs):
        yield fake_chunk

    fake_model = MagicMock()
    fake_model.generate_content_async = MagicMock(return_value=_fake_stream())

    fake_genai = MagicMock()
    fake_genai.GenerativeModel.return_value = fake_model

    with patch.dict(sys.modules, {"google.generativeai": fake_genai}):
        provider = GeminiProvider()
        req = ProviderRequest(
            model="gemini-2.0-flash",
            messages=[Message(role="user", content=[TextBlock(text="read readme")])],
            system=[],
            tools=[],
            max_output_tokens=512,
        )
        events = [e async for e in provider.stream(req)]

    types = [e["type"] for e in events]
    assert "tool_use_start" in types
    assert "tool_use_end" in types

    tool_start = next(e for e in events if e["type"] == "tool_use_start")
    assert tool_start["name"] == "Read"


@pytest.mark.asyncio
async def test_stream_tool_use_deduplicated_across_chunks():
    """The same function_call surfaced in two chunks is emitted only once.

    Gemini sends complete function calls (name + full args). If the SDK
    re-surfaces the same accumulated call in a later chunk, the provider
    must not emit a second tool_use triplet — otherwise the loop assembles
    two ToolUseBlocks and the tool executes twice.
    """
    import sys
    from unittest.mock import MagicMock, patch

    from linch.providers.gemini import GeminiProvider
    from linch.types import Message, ProviderRequest, TextBlock

    def _make_chunk():
        fc = MagicMock()
        fc.name = "Read"
        fc.args = {"file_path": "README.md"}

        part = MagicMock()
        part.text = ""
        part.function_call = fc

        candidate = MagicMock()
        candidate.content.parts = [part]
        candidate.finish_reason = 2

        chunk = MagicMock()
        chunk.candidates = [candidate]
        chunk.usage_metadata.prompt_token_count = 8
        chunk.usage_metadata.candidates_token_count = 3
        return chunk

    async def _fake_stream(*args, **kwargs):
        # Same accumulated function_call surfaced in two consecutive chunks.
        yield _make_chunk()
        yield _make_chunk()

    fake_model = MagicMock()
    fake_model.generate_content_async = MagicMock(return_value=_fake_stream())

    fake_genai = MagicMock()
    fake_genai.GenerativeModel.return_value = fake_model

    with patch.dict(sys.modules, {"google.generativeai": fake_genai}):
        provider = GeminiProvider()
        req = ProviderRequest(
            model="gemini-2.0-flash",
            messages=[Message(role="user", content=[TextBlock(text="read readme")])],
            system=[],
            tools=[],
            max_output_tokens=512,
        )
        events = [e async for e in provider.stream(req)]

    starts = [e for e in events if e["type"] == "tool_use_start"]
    ends = [e for e in events if e["type"] == "tool_use_end"]
    assert len(starts) == 1
    assert len(ends) == 1


@pytest.mark.asyncio
async def test_stream_two_distinct_tool_calls_both_emit():
    """Two different function calls each emit their own tool_use triplet."""
    import sys
    from unittest.mock import MagicMock, patch

    from linch.providers.gemini import GeminiProvider
    from linch.types import Message, ProviderRequest, TextBlock

    def _make_part(name, args):
        fc = MagicMock()
        fc.name = name
        fc.args = args
        part = MagicMock()
        part.text = ""
        part.function_call = fc
        return part

    candidate = MagicMock()
    candidate.content.parts = [
        _make_part("Read", {"file_path": "a.txt"}),
        _make_part("Read", {"file_path": "b.txt"}),
    ]
    candidate.finish_reason = 2

    chunk = MagicMock()
    chunk.candidates = [candidate]
    chunk.usage_metadata.prompt_token_count = 8
    chunk.usage_metadata.candidates_token_count = 3

    async def _fake_stream(*args, **kwargs):
        yield chunk

    fake_model = MagicMock()
    fake_model.generate_content_async = MagicMock(return_value=_fake_stream())

    fake_genai = MagicMock()
    fake_genai.GenerativeModel.return_value = fake_model

    with patch.dict(sys.modules, {"google.generativeai": fake_genai}):
        provider = GeminiProvider()
        req = ProviderRequest(
            model="gemini-2.0-flash",
            messages=[Message(role="user", content=[TextBlock(text="read both")])],
            system=[],
            tools=[],
            max_output_tokens=512,
        )
        events = [e async for e in provider.stream(req)]

    starts = [e for e in events if e["type"] == "tool_use_start"]
    assert len(starts) == 2
    names = {e["name"] for e in starts}
    assert names == {"Read"}
