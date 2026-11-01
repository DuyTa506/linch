from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from linch.providers import OpenAIChatCompletionsProvider
from linch.types import ProviderRequest
from tests.support.providers import OpenAICompatibleFakeClient


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
    provider._client = OpenAICompatibleFakeClient(chunks)

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
    provider._client = OpenAICompatibleFakeClient(chunks)

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
    provider._client = OpenAICompatibleFakeClient(chunks)

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
    provider._client = OpenAICompatibleFakeClient(chunks)

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


async def test_stream_reraises_cancelled_error_without_abort_signal() -> None:
    # A CancelledError with no signal set (or signal not aborted) is an external
    # cancellation, not a cooperative session.abort() — it must propagate as
    # CancelledError, not get reshaped into a plain-Exception AbortError.
    class _CancelledStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise asyncio.CancelledError()

    class _CancelledCompletions:
        async def create(self, **payload):
            return _CancelledStream()

    provider = OpenAIChatCompletionsProvider()
    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=_CancelledCompletions()),
    )

    with pytest.raises(asyncio.CancelledError):
        async for _ in provider.stream(
            ProviderRequest(model="gpt-4o", system=[], tools=[], messages=[])
        ):
            pass


async def test_stream_propagates_wait_for_timeout_instead_of_swallowing_it() -> None:
    # Reproduces the reported bug: without the fix, wait_for's internal
    # task.cancel() got reshaped into AbortError inside the provider, so
    # asyncio.wait_for never saw a CancelledError and returned normally
    # instead of raising TimeoutError.
    class _HangingStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(10)
            raise StopAsyncIteration

    class _HangingCompletions:
        async def create(self, **payload):
            return _HangingStream()

    provider = OpenAIChatCompletionsProvider()
    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=_HangingCompletions()),
    )

    async def consume() -> None:
        async for _ in provider.stream(
            ProviderRequest(model="gpt-4o", system=[], tools=[], messages=[])
        ):
            pass

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(consume(), timeout=0.05)


@pytest.mark.parametrize("method", ["aclose", "close"])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("fails", [False, True])
async def test_chat_client_close_resets_fields_before_close_and_preserves_errors(
    method: str, asynchronous: bool, fails: bool
) -> None:
    from types import SimpleNamespace

    from linch.providers.openai_chat import OpenAIChatCompletionsProvider

    provider = OpenAIChatCompletionsProvider()
    calls = []

    def close() -> None:
        assert provider._client is None
        assert provider._client_loop is None
        calls.append(method)
        if fails:
            raise RuntimeError("close failed")

    async def aclose() -> None:
        close()

    client = SimpleNamespace(**{method: aclose if asynchronous else close})
    if method == "aclose":
        client.close = lambda: pytest.fail("aclose must take precedence")
    provider._client = client
    provider._client_loop = object()
    if fails:
        with pytest.raises(RuntimeError, match="close failed"):
            await provider.aclose()
    else:
        await provider.aclose()
    await provider.aclose()
    assert calls == [method]


@pytest.mark.parametrize(
    "provider_name",
    [
        "OpenAIChatCompletionsProvider",
        "DeepSeekProvider",
        "LlamaCppProvider",
        "VLLMProvider",
        "SGLangProvider",
    ],
)
@pytest.mark.parametrize("arguments", ["", " \n\t"])
@pytest.mark.parametrize("finish_reason", ["tool_calls", "stop"])
async def test_stream_normalizes_empty_tool_arguments(
    provider_name: str, arguments: str, finish_reason: str
) -> None:
    import json

    import linch.providers as providers

    chunks = [
        _chunk(
            delta=SimpleNamespace(
                content=None,
                reasoning_content=None,
                tool_calls=[
                    SimpleNamespace(
                        index=0,
                        id="call_1",
                        function=SimpleNamespace(name="NoArgs", arguments=arguments),
                    )
                ],
            ),
            finish_reason=finish_reason,
        )
    ]
    provider = getattr(providers, provider_name)()
    provider._client = OpenAICompatibleFakeClient(chunks)
    events = [
        event
        async for event in provider.stream(
            ProviderRequest(model="test", system=[], tools=[], messages=[])
        )
    ]
    raw_input = "".join(
        event["json_delta"] for event in events if event["type"] == "tool_use_input_delta"
    )
    assert json.loads(raw_input) == {}
    assert sum(event["type"] == "tool_use_end" for event in events) == 1
    assert events[-1]["stop_reason"] == "tool_use"
