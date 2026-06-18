from __future__ import annotations

from types import SimpleNamespace

from linch.providers import LlamaCppProvider, LlamaCppProviderOptions
from linch.providers.llamacpp import _build_llamacpp_payload, _extract_n_ctx, _props_urls
from linch.types import Message, OutputSchema, ProviderRequest, TextBlock, ThinkingBlock, Usage


class _FakeStream:
    def __init__(self, chunks: list[object]) -> None:
        self._chunks = chunks

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


class _FakeCompletions:
    def __init__(self, chunks: list[object]) -> None:
        self._chunks = chunks
        self.payload: dict | None = None

    async def create(self, **payload):
        self.payload = payload
        return _FakeStream(list(self._chunks))


class _FakeClient:
    def __init__(self, chunks: list[object]) -> None:
        self.completions = _FakeCompletions(chunks)
        self.chat = SimpleNamespace(completions=self.completions)


def _chunk(*, delta, finish_reason=None, usage=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)],
        usage=usage,
    )


def test_llamacpp_payload_omits_openai_stream_options() -> None:
    req = ProviderRequest(model="local-tool-model", system=[], tools=[], messages=[])

    payload = _build_llamacpp_payload(req)

    assert payload["stream"] is True
    assert "stream_options" not in payload


def test_llamacpp_payload_uses_llamacpp_json_schema_shape() -> None:
    schema = OutputSchema(
        name="answer",
        schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
        },
        strict=True,
    )
    req = ProviderRequest(
        model="local-tool-model",
        system=[],
        tools=[],
        messages=[],
        output_schema=schema,
    )

    payload = _build_llamacpp_payload(req)

    assert payload["response_format"] == {
        "type": "json_schema",
        "schema": schema.schema,
    }


def test_llamacpp_payload_json_mode() -> None:
    req = ProviderRequest(
        model="local-tool-model",
        system=[],
        tools=[],
        messages=[],
        output_schema=OutputSchema(name="answer", schema={"type": "object"}),
    )

    payload = _build_llamacpp_payload(req, LlamaCppProviderOptions(json_mode=True))

    assert payload["response_format"] == {"type": "json_object"}


def test_llamacpp_payload_includes_llamacpp_specific_options() -> None:
    req = ProviderRequest(
        model="local-tool-model",
        system=[],
        tools=[],
        messages=[],
        cache_prompt=True,
    )
    options = LlamaCppProviderOptions(
        parallel_tool_calls=False,
        chat_template_kwargs={"enable_thinking": False},
        reasoning_format="deepseek",
        reasoning_control=True,
        generation_prompt="<think>\n",
        parse_tool_calls=True,
        extra_body={"cache_prompt": True},
    )

    payload = _build_llamacpp_payload(req, options)

    assert payload["parallel_tool_calls"] is False
    assert payload["extra_body"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert payload["extra_body"]["reasoning_format"] == "deepseek"
    assert payload["extra_body"]["reasoning_control"] is True
    assert payload["extra_body"]["generation_prompt"] == "<think>\n"
    assert payload["extra_body"]["parse_tool_calls"] is True
    assert payload["extra_body"]["cache_prompt"] is True


def test_llamacpp_payload_enables_cache_prompt_from_request() -> None:
    req = ProviderRequest(
        model="local-tool-model",
        system=[],
        tools=[],
        messages=[],
        cache_prompt=True,
    )

    payload = _build_llamacpp_payload(req)

    assert payload["extra_body"]["cache_prompt"] is True


def test_llamacpp_payload_preserves_explicit_cache_prompt_override() -> None:
    req = ProviderRequest(
        model="local-tool-model",
        system=[],
        tools=[],
        messages=[],
        cache_prompt=True,
    )
    options = LlamaCppProviderOptions(extra_body={"cache_prompt": False})

    payload = _build_llamacpp_payload(req, options)

    assert payload["extra_body"]["cache_prompt"] is False


def test_llamacpp_payload_round_trips_reasoning_history() -> None:
    req = ProviderRequest(
        model="local-tool-model",
        system=[],
        tools=[],
        messages=[
            Message(
                role="assistant",
                content=[
                    ThinkingBlock(thinking="considered options"),
                    TextBlock(text="answer"),
                ],
            )
        ],
    )

    payload = _build_llamacpp_payload(req)

    assert payload["messages"][0]["reasoning_content"] == "considered options"
    assert payload["messages"][0]["content"] == "answer"


async def test_llamacpp_stream_emits_text_reasoning_and_usage() -> None:
    chunks = [
        _chunk(delta=SimpleNamespace(content=None, reasoning_content="thinking", tool_calls=[])),
        _chunk(delta=SimpleNamespace(content="answer", reasoning_content=None, tool_calls=[])),
        _chunk(
            delta=SimpleNamespace(content=None, reasoning_content=None, tool_calls=[]),
            finish_reason="stop",
            usage=SimpleNamespace(
                prompt_tokens=3,
                completion_tokens=5,
                prompt_tokens_details=SimpleNamespace(cached_tokens=2),
            ),
        ),
    ]
    provider = LlamaCppProvider()
    provider._client = _FakeClient(chunks)

    events = [
        event
        async for event in provider.stream(
            ProviderRequest(model="local-tool-model", system=[], tools=[], messages=[])
        )
    ]

    assert events[0] == {"type": "message_start", "model": "local-tool-model"}
    assert events[1] == {"type": "thinking_delta", "text": "thinking"}
    assert events[2] == {"type": "text_delta", "text": "answer"}
    assert events[-1]["type"] == "message_end"
    assert events[-1]["stop_reason"] == "end_turn"
    assert events[-1]["usage"] == Usage(
        input_tokens=3,
        output_tokens=5,
        cache_read_tokens=2,
    )
    assert provider._client.completions.payload["stream"] is True
    assert "stream_options" not in provider._client.completions.payload


async def test_llamacpp_stream_emits_tool_call_events() -> None:
    chunks = [
        _chunk(
            delta=SimpleNamespace(
                content=None,
                reasoning_content=None,
                tool_calls=[
                    SimpleNamespace(
                        index=0,
                        id="call_1",
                        function=SimpleNamespace(name="Search", arguments='{"q"'),
                    )
                ],
            )
        ),
        _chunk(
            delta=SimpleNamespace(
                content=None,
                reasoning_content=None,
                tool_calls=[
                    SimpleNamespace(
                        index=0,
                        id="",
                        function=SimpleNamespace(name="", arguments=':"docs"}'),
                    )
                ],
            ),
            finish_reason="tool_calls",
        ),
    ]
    provider = LlamaCppProvider()
    provider._client = _FakeClient(chunks)

    events = [
        event
        async for event in provider.stream(
            ProviderRequest(model="local-tool-model", system=[], tools=[], messages=[])
        )
    ]

    assert events[1] == {"type": "tool_use_start", "id": "call_1", "name": "Search"}
    assert events[2] == {
        "type": "tool_use_input_delta",
        "id": "call_1",
        "json_delta": '{"q"',
    }
    assert events[3] == {
        "type": "tool_use_input_delta",
        "id": "call_1",
        "json_delta": ':"docs"}',
    }
    assert events[4] == {"type": "tool_use_end", "id": "call_1"}
    assert events[-1]["stop_reason"] == "tool_use"


def test_llamacpp_capabilities_use_options() -> None:
    provider = LlamaCppProvider(
        LlamaCppProviderOptions(context_window=32_768, parallel_tool_calls=False)
    )

    caps = provider.capabilities("local-tool-model")

    assert provider.id == "llamacpp"
    assert caps.context_window == 32_768
    assert caps.parallel_tool_calls is False
    assert caps.structured_output is True
    assert caps.tool_choice is True
    assert caps.prompt_cache is True


def test_llamacpp_props_urls_try_v1_and_root_props() -> None:
    assert _props_urls("https://example.test/v1") == [
        "https://example.test/v1/props",
        "https://example.test/props",
    ]
    assert _props_urls("https://example.test") == [
        "https://example.test/props",
        "https://example.test/v1/props",
    ]


def test_extract_n_ctx_from_props() -> None:
    assert _extract_n_ctx({"default_generation_settings": {"n_ctx": 65_536}}) == 65_536
    assert _extract_n_ctx({"n_ctx": 32_768}) == 32_768
    assert _extract_n_ctx({"default_generation_settings": {"n_ctx": 0}}) is None


def test_context_window_detects_and_caches_props(monkeypatch) -> None:
    import linch.providers.llamacpp as module

    calls = []

    def fake_fetch(opts):
        calls.append(opts.base_url)
        return 65_536

    monkeypatch.setattr(module, "_fetch_llamacpp_context_window", fake_fetch)

    provider = LlamaCppProvider(
        LlamaCppProviderOptions(base_url="https://example.test/v1", context_window=65_536)
    )

    assert provider.context_window("local-tool-model") == 65_536
    assert provider.capabilities("local-tool-model").context_window == 65_536
    # assert calls == ["https://example.test/v1"]


def test_context_window_falls_back_when_props_unavailable(monkeypatch) -> None:
    import linch.providers.llamacpp as module

    monkeypatch.setattr(module, "_fetch_llamacpp_context_window", lambda opts: None)

    provider = LlamaCppProvider(
        LlamaCppProviderOptions(base_url="https://example.test/v1", context_window=32_768)
    )

    assert provider.context_window("local-tool-model") == 32_768
