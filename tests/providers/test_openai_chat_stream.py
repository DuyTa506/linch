from __future__ import annotations

from types import SimpleNamespace

import pytest

from linch.providers import OpenAIChatCompletionsProvider
from linch.types import ProviderRequest


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


async def test_stream_flushes_tool_use_end_on_finish_reason_stop() -> None:
    # llama.cpp and other OpenAI-compatible servers frequently stream a complete
    # tool_calls payload and then close the choice with finish_reason="stop".
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
            finish_reason="stop",
            usage=SimpleNamespace(prompt_tokens=4, completion_tokens=6),
        ),
    ]
    provider = OpenAIChatCompletionsProvider()
    provider._client = _FakeClient(chunks)

    events = [
        event
        async for event in provider.stream(
            ProviderRequest(model="gpt-4o", system=[], tools=[], messages=[])
        )
    ]

    assert events[1] == {"type": "tool_use_start", "id": "call_1", "name": "Search"}
    assert {"type": "tool_use_end", "id": "call_1"} in events
    # tool_use_end emitted exactly once (no double-emit).
    assert sum(1 for e in events if e.get("type") == "tool_use_end") == 1
    assert events[-1]["type"] == "message_end"
    assert events[-1]["stop_reason"] == "tool_use"


async def test_stream_does_not_double_emit_on_finish_reason_tool_calls() -> None:
    chunks = [
        _chunk(
            delta=SimpleNamespace(
                content=None,
                reasoning_content=None,
                tool_calls=[
                    SimpleNamespace(
                        index=0,
                        id="call_1",
                        function=SimpleNamespace(name="Search", arguments='{"q":"x"}'),
                    )
                ],
            ),
            finish_reason="tool_calls",
        ),
    ]
    provider = OpenAIChatCompletionsProvider()
    provider._client = _FakeClient(chunks)

    events = [
        event
        async for event in provider.stream(
            ProviderRequest(model="gpt-4o", system=[], tools=[], messages=[])
        )
    ]

    assert sum(1 for e in events if e.get("type") == "tool_use_end") == 1
    assert events[-1]["stop_reason"] == "tool_use"


async def test_stream_text_only_unaffected() -> None:
    chunks = [
        _chunk(
            delta=SimpleNamespace(content="hi", reasoning_content=None, tool_calls=[]),
        ),
        _chunk(
            delta=SimpleNamespace(content=None, reasoning_content=None, tool_calls=[]),
            finish_reason="stop",
            usage=SimpleNamespace(
                prompt_tokens=10,
                completion_tokens=2,
                prompt_tokens_details=SimpleNamespace(cached_tokens=6),
            ),
        ),
    ]
    provider = OpenAIChatCompletionsProvider()
    provider._client = _FakeClient(chunks)

    events = [
        event
        async for event in provider.stream(
            ProviderRequest(model="gpt-4o", system=[], tools=[], messages=[])
        )
    ]

    assert {"type": "text_delta", "text": "hi"} in events
    assert not any(e.get("type") == "tool_use_end" for e in events)
    assert events[-1]["stop_reason"] == "end_turn"
    assert events[-1]["usage"].cache_read_tokens == 6


async def test_stream_captures_usage_from_final_empty_choices_chunk() -> None:
    # Per the OpenAI streaming spec, when stream_options.include_usage is set the
    # server emits token usage in a FINAL chunk whose `choices` list is empty.
    # vLLM/SGLang/OpenAI all do this, so the provider must read usage before the
    # empty-choices gate or it reports zero tokens for every streamed turn.
    chunks = [
        _chunk(
            delta=SimpleNamespace(content="hi", reasoning_content=None, tool_calls=[]),
            finish_reason="stop",
        ),
        SimpleNamespace(
            choices=[],
            usage=SimpleNamespace(
                prompt_tokens=37,
                completion_tokens=512,
                prompt_tokens_details=SimpleNamespace(cached_tokens=11),
            ),
        ),
    ]
    provider = OpenAIChatCompletionsProvider()
    provider._client = _FakeClient(chunks)

    events = [
        event
        async for event in provider.stream(
            ProviderRequest(model="gpt-4o", system=[], tools=[], messages=[])
        )
    ]

    end = events[-1]
    assert end["type"] == "message_end"
    assert end["usage"].input_tokens == 37
    assert end["usage"].output_tokens == 512
    assert end["usage"].cache_read_tokens == 11


async def test_stream_maps_aborted_mid_stream_failure_to_abort_error() -> None:
    from linch.abort import AbortContext
    from linch.errors import AbortError

    class _BrokenStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise RuntimeError("connection closed")

    class _BrokenCompletions:
        async def create(self, **payload):
            return _BrokenStream()

    signal = AbortContext()
    signal.abort()
    provider = OpenAIChatCompletionsProvider()
    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=_BrokenCompletions()),
    )

    with pytest.raises(AbortError):
        async for _ in provider.stream(
            ProviderRequest(model="gpt-4o", system=[], tools=[], messages=[], signal=signal)
        ):
            pass
