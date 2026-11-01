"""Gemini provider contract tests; streaming uses a fake google-genai client."""

from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Unit: import, instantiation, and model limits
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
    assert opts.project is None
    assert opts.location == "us-central1"


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


def test_gemini_capabilities_prompt_cache():
    """GeminiProvider preserves cache intent and reports cached token usage."""
    from linch.providers.gemini import GeminiProvider

    caps = GeminiProvider().capabilities("gemini-2.5-pro")
    assert caps.prompt_cache is True


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
    assert gemini_tools[0]["parameters_json_schema"] == tools[0]["input_schema"]


class FakeClient:
    """Only the SDK's transport is replaced; configs and chunks use real types."""

    def __init__(self) -> None:
        self.aio = self
        self.models = self
        self.chunks: list[Any] = []
        self.calls: list[dict[str, Any]] = []
        self.options: list[dict[str, Any]] = []
        self.open_error: BaseException | None = None
        self.stream_error: BaseException | None = None
        self.started = asyncio.Event()
        self.block = False
        self.stream_closed = False
        self.async_closes = 0
        self.sync_closes = 0

    async def generate_content_stream(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.open_error:
            raise self.open_error
        return self._stream()

    async def _stream(self) -> Any:
        try:
            for chunk in self.chunks:
                yield chunk
            self.started.set()
            if self.block:
                await asyncio.Event().wait()
            if self.stream_error:
                raise self.stream_error
        finally:
            self.stream_closed = True

    async def aclose(self) -> None:
        self.async_closes += 1

    def close(self) -> None:
        self.sync_closes += 1


@pytest.fixture
def sdk() -> Any:
    return pytest.importorskip("google.genai")


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch, sdk: Any) -> FakeClient:
    client = FakeClient()

    def create(**kwargs: Any) -> FakeClient:
        client.options.append(kwargs)
        return client

    monkeypatch.setattr(sdk, "Client", create)
    return client


def request(**kwargs: Any) -> Any:
    from linch.types import Message, ProviderRequest, TextBlock

    return ProviderRequest(
        model=kwargs.pop("model", "gemini-2.5-flash"),
        system=kwargs.pop("system", []),
        messages=kwargs.pop("messages", [Message(role="user", content=[TextBlock(text="hi")])]),
        tools=kwargs.pop("tools", []),
        **kwargs,
    )


def chunk(sdk: Any, parts: list[dict[str, Any]], finish: str | None = None, **usage: int) -> Any:
    return sdk.types.GenerateContentResponse(
        candidates=[
            sdk.types.Candidate(content=sdk.types.Content(parts=parts), finish_reason=finish)
        ],
        usage_metadata=sdk.types.GenerateContentResponseUsageMetadata(**usage) if usage else None,
    )


async def test_stream_text_response(fake_client: FakeClient, sdk: Any) -> None:
    from linch.providers.gemini import GeminiProvider
    from linch.types import Usage

    fake_client.chunks = [
        chunk(sdk, [{"text": "Paris "}]),
        chunk(
            sdk,
            [{"text": "is the capital."}],
            "STOP",
            prompt_token_count=10,
            candidates_token_count=5,
            cached_content_token_count=4,
        ),
        sdk.types.GenerateContentResponse(),
    ]
    events = [e async for e in GeminiProvider().stream(request())]
    assert events[0] == {"type": "message_start", "model": "gemini-2.5-flash"}
    assert [e["text"] for e in events if e["type"] == "text_delta"] == ["Paris ", "is the capital."]
    assert events[-1]["stop_reason"] == "end_turn"
    assert events[-1]["usage"] == Usage(input_tokens=10, output_tokens=5, cache_read_tokens=4)
    assert events[-1]["provider_metadata"] is None
    assert fake_client.stream_closed


@pytest.mark.parametrize(
    "parts, count",
    [
        ([{"function_call": {"name": "Read", "args": {"path": "a"}}}], 1),
        (
            [
                {"function_call": {"name": "Read", "args": {"path": "a"}}},
                {"function_call": {"name": "Read", "args": {"path": "b"}}},
            ],
            2,
        ),
        (
            [
                {"function_call": {"id": "one", "name": "Read", "args": {}}},
                {"function_call": {"id": "two", "name": "Read", "args": {}}},
            ],
            2,
        ),
    ],
)
async def test_stream_tool_calls_deduplicated_across_chunks(
    fake_client: FakeClient, sdk: Any, parts: list[dict[str, Any]], count: int
) -> None:
    from linch.providers.gemini import GeminiProvider

    fake_client.chunks = [chunk(sdk, parts), chunk(sdk, parts, "STOP")]
    events = [e async for e in GeminiProvider().stream(request())]
    starts = [e for e in events if e["type"] == "tool_use_start"]
    inputs = [e for e in events if e["type"] == "tool_use_input_delta"]
    ends = [e for e in events if e["type"] == "tool_use_end"]
    assert len(starts) == len(inputs) == len(ends) == count
    for start, delta, end, part in zip(starts, inputs, ends, parts, strict=True):
        assert start["name"] == "Read"
        assert start["id"] == delta["id"] == end["id"]
        assert json.loads(delta["json_delta"]) == part["function_call"]["args"]
        if "id" in part["function_call"]:
            assert start["id"] == part["function_call"]["id"]
    assert events[-1]["stop_reason"] == "tool_use"


async def test_stream_builds_typed_request(fake_client: FakeClient, sdk: Any) -> None:
    from linch.providers.gemini import GeminiProvider, GeminiProviderOptions
    from linch.types import OutputSchema, SystemBlock

    options = GeminiProviderOptions(
        api_key="test-key", default_generation_config={"temperature": 0.8, "top_p": 0.7}
    )
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }
    req = request(
        system=[SystemBlock(text="first"), SystemBlock(text="second")],
        max_output_tokens=256,
        temperature=0.2,
        stop_sequences=["END"],
        output_schema=OutputSchema(name="answer", schema=schema),
        tools=[{"name": "Read", "description": "Read a file", "input_schema": schema}],
        tool_choice={"name": "Read"},
    )
    _ = [e async for e in GeminiProvider(options).stream(req)]
    assert len(fake_client.options) == 1
    assert fake_client.options[0]["api_key"] == "test-key"
    call = fake_client.calls[0]
    assert call["model"] == "gemini-2.5-flash"
    assert isinstance(call["contents"][0], sdk.types.Content)
    assert call["contents"][0].role == "user"
    assert call["contents"][0].parts[0].text == "hi"
    config = call["config"]
    assert isinstance(config, sdk.types.GenerateContentConfig)
    assert config.system_instruction == "first\n\nsecond"
    assert config.max_output_tokens == 256
    assert config.temperature == 0.2
    assert config.stop_sequences == ["END"]
    assert config.top_p == 0.7
    assert config.response_mime_type == "application/json"
    assert config.response_json_schema == schema
    declaration = config.tools[0].function_declarations[0]
    assert declaration.name == "Read"
    assert declaration.description == "Read a file"
    assert declaration.parameters_json_schema == schema
    assert config.tool_config.function_calling_config.mode == "ANY"
    assert config.tool_config.function_calling_config.allowed_function_names == ["Read"]
    assert config.automatic_function_calling.disable is True
    assert options.default_generation_config == {"temperature": 0.8, "top_p": 0.7}


async def test_vertex_uses_project_and_location(fake_client: FakeClient) -> None:
    from linch.providers.gemini import GeminiProvider, GeminiProviderOptions

    provider = GeminiProvider(
        GeminiProviderOptions(project="tenant-project", location="europe-west4")
    )
    _ = [e async for e in provider.stream(request())]
    opts = fake_client.options[0]
    assert opts["vertexai"] is True
    assert opts["project"] == "tenant-project"
    assert opts["location"] == "europe-west4"
    assert "api_key" not in opts


@pytest.mark.parametrize(
    "thinking",
    [
        {"thinking_budget": 128, "include_thoughts": True},
        {"thinking_budget": 0},
        {"thinking_level": "HIGH"},
    ],
)
async def test_native_thinking_override(fake_client: FakeClient, thinking: dict[str, Any]) -> None:
    from linch.providers.gemini import GeminiProvider, GeminiProviderOptions

    options = GeminiProviderOptions(
        default_generation_config={"thinking_config": {"thinking_budget": -1}}
    )
    _ = [e async for e in GeminiProvider(options).stream(request(thinking=thinking))]
    assert fake_client.calls[0]["config"].thinking_config.model_dump(exclude_none=True) == thinking
    assert options.default_generation_config == {"thinking_config": {"thinking_budget": -1}}


@pytest.mark.parametrize(
    "thinking, expected",
    [
        ({"type": "disabled"}, {"thinking_budget": 0}),
        ({"type": "enabled", "budget_tokens": 128}, {"thinking_budget": 128}),
        ({"type": "enabled"}, {"thinking_budget": -1}),
        ({"type": "adaptive"}, {"thinking_budget": -1}),
        (
            {"type": "enabled", "budget_tokens": 128, "display": "omitted"},
            {"thinking_budget": 128, "include_thoughts": False},
        ),
    ],
)
async def test_shared_thinking_config(
    fake_client: FakeClient, thinking: dict[str, Any], expected: dict[str, Any]
) -> None:
    from linch.providers.gemini import GeminiProvider

    _ = [e async for e in GeminiProvider().stream(request(thinking=thinking))]
    assert fake_client.calls[0]["config"].thinking_config.model_dump(exclude_none=True) == expected


async def test_repeated_call_can_supply_signature_later(fake_client: FakeClient, sdk: Any) -> None:
    from linch.providers.gemini import GeminiProvider

    call = {"function_call": {"name": "Read", "args": {}}}
    fake_client.chunks = [
        chunk(sdk, [call]),
        chunk(sdk, [{**call, "thought_signature": b"late"}], "STOP"),
    ]
    events = [e async for e in GeminiProvider().stream(request())]
    assert len([e for e in events if e["type"] == "tool_use_start"]) == 1
    parts = events[-1]["provider_metadata"]["gemini_parts"]
    assert len(parts) == 1
    assert sdk.types.Part(**parts[0]).thought_signature == b"late"


async def test_thinking_usage_and_signature_replay(fake_client: FakeClient, sdk: Any) -> None:
    from linch.providers.gemini import GeminiProvider
    from linch.types import Message, ThinkingBlock, ToolResultBlock, ToolUseBlock, Usage

    fake_client.chunks = [
        chunk(
            sdk,
            [
                {"thought": True, "text": "Let me read."},
                {"function_call": {"name": "Read", "args": {}}, "thought_signature": b"signature"},
            ],
            "STOP",
            prompt_token_count=10,
            candidates_token_count=2,
            thoughts_token_count=8,
        )
    ]
    provider = GeminiProvider()
    events = [e async for e in provider.stream(request())]
    assert {"type": "thinking_delta", "text": "Let me read."} in events
    assert not any(e["type"] == "text_delta" for e in events)
    assert events[-1]["usage"] == Usage(input_tokens=10, output_tokens=10)
    tid = next(e["id"] for e in events if e["type"] == "tool_use_start")
    metadata = json.loads(json.dumps(events[-1]["provider_metadata"]))
    assistant = Message(
        role="assistant",
        content=[
            ThinkingBlock(thinking="Let me read."),
            ToolUseBlock(id=tid, name="Read", input={}),
        ],
        provider_metadata=metadata,
    )
    fake_client.chunks = []
    _ = [
        e
        async for e in provider.stream(
            request(
                messages=[
                    assistant,
                    Message(
                        role="user", content=[ToolResultBlock(tool_use_id=tid, content="file")]
                    ),
                ]
            )
        )
    ]
    parts = fake_client.calls[1]["contents"][0].parts
    assert parts[0].thought is True
    assert parts[1].thought_signature == b"signature"
    assert parts[1].function_call.id == tid
    result = fake_client.calls[1]["contents"][1].parts[0].function_response
    assert result.name == "Read"
    assert result.id == tid
    assert result.response == {"content": "file", "is_error": False}


@pytest.mark.parametrize(
    "finish, expected",
    [
        ("MAX_TOKENS", "max_tokens"),
        ("STOP", "end_turn"),
        ("SAFETY", "refusal"),
        ("RECITATION", "refusal"),
    ],
)
async def test_finish_reasons(
    fake_client: FakeClient, sdk: Any, finish: str, expected: str
) -> None:
    from linch.providers.gemini import GeminiProvider

    fake_client.chunks = [chunk(sdk, [], finish)]
    events = [e async for e in GeminiProvider().stream(request())]
    assert events[-1]["stop_reason"] == expected


@pytest.mark.parametrize(
    "parts",
    [[], [{"text": "partial"}], [{"function_call": {"name": "Read", "args": {}}}]],
)
async def test_malformed_function_call_is_retryable_error(
    fake_client: FakeClient, sdk: Any, parts: list[dict[str, Any]]
) -> None:
    from linch.errors import ProviderError
    from linch.providers.gemini import GeminiProvider

    fake_client.chunks = [
        chunk(sdk, [{"text": "earlier partial output"}]),
        chunk(sdk, parts, "MALFORMED_FUNCTION_CALL"),
    ]
    events = []
    with pytest.raises(ProviderError, match="malformed function call") as info:
        async for event in GeminiProvider().stream(request()):
            events.append(event)
    assert info.value.retryable
    assert not any(event["type"] == "message_end" for event in events)
    assert fake_client.stream_closed


async def test_tool_calls_override_later_token_limit(fake_client: FakeClient, sdk: Any) -> None:
    from linch.providers.gemini import GeminiProvider

    fake_client.chunks = [
        chunk(sdk, [{"function_call": {"name": "Read", "args": {}}}]),
        chunk(sdk, [], "MAX_TOKENS"),
    ]
    events = [e async for e in GeminiProvider().stream(request())]
    assert events[-1]["stop_reason"] == "tool_use"


async def test_usage_only_chunks_preserve_omitted_fields(fake_client: FakeClient, sdk: Any) -> None:
    from linch.providers.gemini import GeminiProvider
    from linch.types import Usage

    fake_client.chunks = [
        chunk(
            sdk, [], prompt_token_count=10, candidates_token_count=7, cached_content_token_count=4
        ),
        sdk.types.GenerateContentResponse(
            usage_metadata=sdk.types.GenerateContentResponseUsageMetadata(
                candidates_token_count=0, thoughts_token_count=2
            )
        ),
    ]
    events = [e async for e in GeminiProvider().stream(request())]
    assert events[-1]["usage"] == Usage(input_tokens=10, output_tokens=2, cache_read_tokens=4)


@pytest.mark.parametrize("stage", ["open", "stream"])
@pytest.mark.parametrize(
    "code, message, error_name, retryable",
    [
        (401, "unauthenticated", "AuthError", False),
        (403, "permission denied", "AuthError", False),
        (429, "resource exhausted", "RateLimitError", True),
        (429, "quota exceeded for API key", "RateLimitError", True),
        (400, "input token count exceeds maximum tokens allowed", "ContextLengthError", False),
        (400, "invalid temperature", "ProviderError", False),
        (400, "max_output_tokens exceeds limit", "ProviderError", False),
        (408, "deadline exceeded", "ProviderError", True),
        (500, "internal", "ProviderError", True),
        (503, "service unavailable", "ProviderError", True),
        (503, "temporarily unable to check credentials", "ProviderError", True),
    ],
)
async def test_sdk_errors_mapped(
    fake_client: FakeClient,
    sdk: Any,
    stage: str,
    code: int,
    message: str,
    error_name: str,
    retryable: bool,
) -> None:
    from linch import errors
    from linch.providers.gemini import GeminiProvider

    error = sdk.errors.APIError(code, {"error": {"message": message}})
    setattr(fake_client, f"{stage}_error", error)
    events = []
    with pytest.raises(getattr(errors, error_name)) as info:
        async for event in GeminiProvider().stream(request()):
            events.append(event)
    assert info.value.retryable is retryable
    if error_name == "ProviderError":
        assert info.value.status == code
    assert info.value.__cause__ is error
    assert not any(e["type"] == "message_end" for e in events)


async def test_retry_after_from_sdk_response(fake_client: FakeClient, sdk: Any) -> None:
    from linch.errors import RateLimitError
    from linch.providers.gemini import GeminiProvider

    fake_client.open_error = sdk.errors.ClientError(
        429, {"message": "quota"}, response=SimpleNamespace(headers={"Retry-After": "2.5"})
    )
    with pytest.raises(RateLimitError) as info:
        _ = [e async for e in GeminiProvider().stream(request())]
    assert info.value.retry_after_seconds == 2.5


@pytest.mark.parametrize("error", [TimeoutError("timeout"), ConnectionError("disconnected")])
async def test_transport_errors_retryable(fake_client: FakeClient, error: Exception) -> None:
    from linch.errors import ProviderError
    from linch.providers.gemini import GeminiProvider

    fake_client.open_error = error
    with pytest.raises(ProviderError) as info:
        _ = [e async for e in GeminiProvider().stream(request())]
    assert info.value.retryable is True


async def test_invalid_config_is_nonretryable(fake_client: FakeClient) -> None:
    from linch.errors import ProviderError
    from linch.providers.gemini import GeminiProvider, GeminiProviderOptions

    with pytest.raises(ProviderError) as info:
        _ = [
            e
            async for e in GeminiProvider(
                GeminiProviderOptions(default_generation_config={"invalid_option": True})
            ).stream(request())
        ]
    assert info.value.retryable is False
    assert not fake_client.calls


async def test_missing_sdk_has_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    from linch.errors import ProviderError
    from linch.providers.gemini import GeminiProvider

    monkeypatch.setitem(sys.modules, "google.genai", None)
    with pytest.raises(ProviderError, match="google-genai"):
        _ = [e async for e in GeminiProvider().stream(request())]


async def test_external_cancellation_closes_stream(fake_client: FakeClient) -> None:
    from linch.providers.gemini import GeminiProvider

    fake_client.block = True

    async def consume() -> None:
        _ = [e async for e in GeminiProvider().stream(request())]

    task = asyncio.create_task(consume())
    await asyncio.wait_for(fake_client.started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fake_client.stream_closed


async def test_consumer_close_closes_stream(fake_client: FakeClient, sdk: Any) -> None:
    from linch.providers.gemini import GeminiProvider

    fake_client.chunks = [chunk(sdk, [{"text": "hello"}])]
    stream = GeminiProvider().stream(request())
    assert (await anext(stream))["type"] == "message_start"
    assert (await anext(stream))["type"] == "text_delta"
    await stream.aclose()
    assert fake_client.stream_closed


async def test_client_reuse_and_idempotent_close(fake_client: FakeClient) -> None:
    from linch.providers.gemini import GeminiProvider

    provider = GeminiProvider()
    await provider.aclose()
    _ = [e async for e in provider.stream(request())]
    _ = [e async for e in provider.stream(request())]
    assert len(fake_client.options) == 1
    await provider.aclose()
    await provider.aclose()
    assert fake_client.async_closes == fake_client.sync_closes == 1


def test_gemini_cached_tokens_surfaced_from_usage_metadata() -> None:
    from linch._prompt_cache import gemini_cached_tokens

    assert gemini_cached_tokens(SimpleNamespace(cached_content_token_count=512)) == 512
    assert gemini_cached_tokens(SimpleNamespace(cached_content_token_count=None)) == 0
    assert gemini_cached_tokens(object()) == 0
