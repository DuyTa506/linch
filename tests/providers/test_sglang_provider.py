from __future__ import annotations

from types import SimpleNamespace

from linch.providers import SGLangProvider, SGLangProviderOptions
from linch.providers.sglang import _build_sglang_payload
from linch.types import OutputSchema, ProviderRequest, Usage


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


def test_sglang_capabilities_and_provider_id() -> None:
    provider = SGLangProvider(
        SGLangProviderOptions(context_window=32_768, parallel_tool_calls=False)
    )
    caps = provider.capabilities("served-model")

    assert provider.id == "sglang"
    assert caps.context_window == 32_768
    assert caps.parallel_tool_calls is False
    assert caps.structured_output is True
    assert caps.tool_choice is True
    assert caps.prompt_cache is True


def test_sglang_payload_includes_sglang_extras_and_stream_options_by_default() -> None:
    req = ProviderRequest(
        model="served-model",
        system=[],
        tools=[],
        messages=[],
        output_schema=OutputSchema(name="answer", schema={"type": "object"}),
    )
    options = SGLangProviderOptions(
        parallel_tool_calls=True,
        sampling_params={"top_p": 0.9},
        enable_cache_report=True,
        extra_body={"sampling_params": {"top_p": 0.8}, "custom": "value"},
    )

    payload = _build_sglang_payload(req, options)

    assert payload["stream"] is True
    # stream_options is on by default so SGLang returns token usage (incl. cache).
    assert payload["stream_options"] == {"include_usage": True}
    assert payload["parallel_tool_calls"] is True
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["extra_body"] == {
        "sampling_params": {"top_p": 0.8},
        "enable_cache_report": True,
        "custom": "value",
    }


def test_sglang_payload_can_omit_stream_options() -> None:
    req = ProviderRequest(model="served-model", system=[], tools=[], messages=[])

    payload = _build_sglang_payload(
        req,
        SGLangProviderOptions(include_stream_options=False),
    )

    assert payload["stream"] is True
    assert "stream_options" not in payload


async def test_sglang_stream_text_tool_reasoning_and_cache_usage() -> None:
    chunks = [
        _chunk(delta=SimpleNamespace(content=None, reasoning_content="think", tool_calls=[])),
        _chunk(delta=SimpleNamespace(content="answer", reasoning_content=None, tool_calls=[])),
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
            finish_reason="stop",
            usage=SimpleNamespace(
                prompt_tokens=8,
                completion_tokens=13,
                cached_tokens=5,
            ),
        ),
    ]
    provider = SGLangProvider(SGLangProviderOptions(enable_cache_report=True))
    provider._client = _FakeClient(chunks)

    events = [
        event
        async for event in provider.stream(
            ProviderRequest(model="served-model", system=[], tools=[], messages=[])
        )
    ]

    assert events[1] == {"type": "thinking_delta", "text": "think"}
    assert events[2] == {"type": "text_delta", "text": "answer"}
    assert events[3] == {"type": "tool_use_start", "id": "call_1", "name": "Search"}
    assert {"type": "tool_use_end", "id": "call_1"} in events
    assert events[-1]["stop_reason"] == "tool_use"
    assert events[-1]["usage"] == Usage(
        input_tokens=8,
        output_tokens=13,
        cache_read_tokens=5,
    )
    assert provider._client.completions.payload["stream_options"] == {"include_usage": True}
    assert provider._client.completions.payload["extra_body"]["enable_cache_report"] is True
