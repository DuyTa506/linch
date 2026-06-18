from __future__ import annotations

from types import SimpleNamespace

import pytest

from linch.providers import VLLMProvider, VLLMProviderOptions
from linch.providers.vllm import _build_vllm_payload
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


def test_vllm_capabilities_and_provider_id() -> None:
    provider = VLLMProvider(VLLMProviderOptions(context_window=65_536, parallel_tool_calls=False))
    caps = provider.capabilities("served-model")

    assert provider.id == "vllm"
    assert caps.context_window == 65_536
    assert caps.parallel_tool_calls is False
    assert caps.structured_output is True
    assert caps.tool_choice is True
    assert caps.prompt_cache is True


def test_vllm_payload_includes_openai_compatible_controls() -> None:
    schema = OutputSchema(name="answer", schema={"type": "object"}, strict=True)
    req = ProviderRequest(
        model="served-model",
        system=[],
        messages=[
            Message(
                role="assistant",
                content=[ThinkingBlock(thinking="reasoning"), TextBlock(text="answer")],
            )
        ],
        tools=[
            {
                "name": "Search",
                "description": "Search docs",
                "input_schema": {"type": "object"},
            }
        ],
        output_schema=schema,
        tool_choice={"name": "Search"},
    )

    payload = _build_vllm_payload(
        req,
        VLLMProviderOptions(
            parallel_tool_calls=False,
            extra_body={"guided_decoding_backend": "outlines"},
        ),
    )

    assert payload["stream"] is True
    assert payload["stream_options"] == {"include_usage": True}
    assert payload["parallel_tool_calls"] is False
    assert payload["extra_body"] == {"guided_decoding_backend": "outlines"}
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["tool_choice"]["function"]["name"] == "Search"
    assert payload["messages"][0]["reasoning_content"] == "reasoning"


async def test_vllm_stream_reuses_openai_compatible_parser() -> None:
    chunks = [
        _chunk(delta=SimpleNamespace(content=None, reasoning_content="thinking", tool_calls=[])),
        _chunk(delta=SimpleNamespace(content="answer", reasoning_content=None, tool_calls=[])),
        _chunk(
            delta=SimpleNamespace(
                content=None,
                reasoning_content=None,
                tool_calls=[
                    SimpleNamespace(
                        index=0,
                        id="call_1",
                        function=SimpleNamespace(name="Search", arguments='{"q":"docs"}'),
                    )
                ],
            ),
            finish_reason="tool_calls",
            usage=SimpleNamespace(
                prompt_tokens=3,
                completion_tokens=5,
                prompt_tokens_details=SimpleNamespace(cached_tokens=2),
            ),
        ),
    ]
    provider = VLLMProvider()
    provider._client = _FakeClient(chunks)

    events = [
        event
        async for event in provider.stream(
            ProviderRequest(model="served-model", system=[], tools=[], messages=[])
        )
    ]

    assert events[1] == {"type": "thinking_delta", "text": "thinking"}
    assert events[2] == {"type": "text_delta", "text": "answer"}
    assert {"type": "tool_use_start", "id": "call_1", "name": "Search"} in events
    assert {"type": "tool_use_end", "id": "call_1"} in events
    assert events[-1]["stop_reason"] == "tool_use"
    assert events[-1]["usage"] == Usage(
        input_tokens=3,
        output_tokens=5,
        cache_read_tokens=2,
    )


async def test_vllm_stream_maps_aborted_create_failure_to_abort_error() -> None:
    from linch.abort import AbortContext
    from linch.errors import AbortError

    class _BrokenCompletions:
        async def create(self, **payload):
            raise RuntimeError("connection closed")

    signal = AbortContext()
    signal.abort()
    provider = VLLMProvider()
    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=_BrokenCompletions()),
    )

    with pytest.raises(AbortError):
        async for _ in provider.stream(
            ProviderRequest(model="served-model", system=[], tools=[], messages=[], signal=signal)
        ):
            pass
