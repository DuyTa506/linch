from __future__ import annotations

from types import SimpleNamespace

from linch.providers import SGLangProvider, SGLangProviderOptions
from linch.providers.sglang import _build_sglang_payload
from linch.types import OutputSchema, ProviderRequest, Usage
from tests.support.providers import OpenAICompatibleFakeClient


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


def test_sglang_reasoning_and_thinking_options_are_explicit() -> None:
    options = SGLangProviderOptions(
        chat_template_kwargs={"enable_thinking": True},
        disable_thinking_kwargs={"enable_thinking": False},
    )
    provider = SGLangProvider(options)
    assert provider.capabilities("served-model").can_disable_thinking is True

    req = ProviderRequest(
        model="served-model",
        system=[],
        tools=[],
        messages=[],
        thinking={"type": "disabled"},
    )
    payload = _build_sglang_payload(req, options)
    assert payload["extra_body"]["chat_template_kwargs"] == {"enable_thinking": False}


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
        sampling_params={"top_p": 0.9, "top_k": 50},
        enable_cache_report=True,
        extra_body={"top_p": 0.8, "top_k": 10, "custom": "value"},
    )

    payload = _build_sglang_payload(req, options)

    assert payload["stream"] is True
    # stream_options is on by default so SGLang returns token usage (incl. cache).
    assert payload["stream_options"] == {"include_usage": True}
    assert payload["parallel_tool_calls"] is True
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["extra_body"] == {
        "top_p": 0.8,
        "top_k": 10,
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
    provider._client = OpenAICompatibleFakeClient(chunks)

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
        input_tokens=3,
        output_tokens=13,
        cache_read_tokens=5,
    )
    assert provider._client.completions.payload["stream_options"] == {"include_usage": True}
    assert "enable_cache_report" not in provider._client.completions.payload.get("extra_body", {})


async def test_sglang_missing_cache_report_warns_once_per_provider(caplog) -> None:
    usage = SimpleNamespace(prompt_tokens=4, completion_tokens=1)
    chunks = [
        _chunk(
            delta=SimpleNamespace(content="a", reasoning_content=None, tool_calls=[]),
            usage=usage,
        ),
        _chunk(
            delta=SimpleNamespace(content="b", reasoning_content=None, tool_calls=[]),
            usage=usage,
        ),
    ]
    provider = SGLangProvider(SGLangProviderOptions(enable_cache_report=True))
    provider._client = OpenAICompatibleFakeClient(chunks)
    with caplog.at_level("WARNING"):
        _ = [
            event
            async for event in provider.stream(
                ProviderRequest(model="m", system=[], tools=[], messages=[])
            )
        ]
    records = [r for r in caplog.records if "--enable-cache-report" in r.message]
    assert len(records) == 1


def test_sglang_legacy_positional_options_keep_wire_fields() -> None:
    options = SGLangProviderOptions(
        None,
        "https://server/v1",
        {"X-Test": "yes"},
        65_536,
        False,
        False,
        False,
        {"top_k": 50},
        True,
        {"top_k": 10},
    )
    payload = _build_sglang_payload(
        ProviderRequest(
            model="m",
            system=[],
            tools=[],
            messages=[],
            output_schema=OutputSchema(name="answer", schema={"type": "object"}),
        ),
        options,
    )

    assert "stream_options" not in payload
    assert payload["parallel_tool_calls"] is False
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["extra_body"] == {"top_k": 10}
