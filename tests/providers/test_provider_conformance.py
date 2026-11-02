"""Provider conformance suite.

Two layers:

- ``assert_provider_contract`` exercises the uniform lifecycle/declarative
  contract (id, context window, capabilities shape, idempotent transport close)
  for every built-in provider without a live transport.
- Per-provider request-shape checks enforce the rule that *a provider must not
  advertise a capability its request builder ignores*: every provider that
  declares ``tool_choice`` must actually map a forced tool choice into its
  outgoing request where that wire combination is supported. DeepSeek thinking
  explicitly disallows forced selection, so its mapping check uses thinking
  disabled; its native constraint is tested separately. Transport closers for
  Anthropic and OpenAI Responses (added in this phase) are checked for effect
  and idempotency.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from linch.testing import assert_provider_contract
from linch.types import Message, ProviderRequest, TextBlock


def _all_providers() -> list[tuple[str, Any, str]]:
    from linch.providers.anthropic import AnthropicProvider
    from linch.providers.deepseek import DeepSeekProvider
    from linch.providers.gemini import GeminiProvider
    from linch.providers.llamacpp import LlamaCppProvider
    from linch.providers.openai_chat import OpenAIChatCompletionsProvider
    from linch.providers.openai_responses import OpenAIResponsesProvider
    from linch.providers.sglang import SGLangProvider
    from linch.providers.vllm import VLLMProvider

    return [
        ("anthropic", AnthropicProvider, "claude-sonnet-4-5"),
        ("deepseek", DeepSeekProvider, "deepseek-v4-flash"),
        ("openai-chat", OpenAIChatCompletionsProvider, "gpt-4o"),
        ("openai-responses", OpenAIResponsesProvider, "gpt-4o"),
        ("gemini", GeminiProvider, "gemini-2.0-flash"),
        ("llamacpp", LlamaCppProvider, "local-model"),
        ("vllm", VLLMProvider, "local-model"),
        ("sglang", SGLangProvider, "local-model"),
    ]


@pytest.mark.parametrize("name,cls,model", _all_providers(), ids=[p[0] for p in _all_providers()])
async def test_provider_satisfies_lifecycle_contract(name: str, cls: Any, model: str) -> None:
    await assert_provider_contract(lambda: cls(), model=model)


def _forced_tool_request() -> ProviderRequest:
    return ProviderRequest(
        model="m",
        system=[],
        tools=[
            {
                "name": "Weather",
                "description": "d",
                "input_schema": {"type": "object", "properties": {}},
            }
        ],
        messages=[Message(role="user", content=[TextBlock(text="hi")])],
        tool_choice={"name": "Weather"},
    )


@pytest.mark.parametrize(
    "provider_key",
    ["anthropic", "responses", "chat", "llamacpp", "vllm", "sglang", "deepseek"],
)
def test_forced_tool_choice_payload_mapping(provider_key: str) -> None:
    req = _forced_tool_request()
    if provider_key == "anthropic":
        from linch.providers.anthropic import AnthropicProviderOptions, _build_payload

        assert _build_payload(req, AnthropicProviderOptions())["tool_choice"] == {
            "type": "tool",
            "name": "Weather",
        }
        return
    if provider_key == "responses":
        from linch.openai_responses import build_payload

        assert build_payload(req)["tool_choice"] == {"type": "function", "name": "Weather"}
        return

    from linch.providers.deepseek import DeepSeekProvider, DeepSeekProviderOptions
    from linch.providers.llamacpp import LlamaCppProvider
    from linch.providers.openai_chat import OpenAIChatCompletionsProvider
    from linch.providers.sglang import SGLangProvider
    from linch.providers.vllm import VLLMProvider

    providers = {
        "chat": OpenAIChatCompletionsProvider(),
        "llamacpp": LlamaCppProvider(),
        "vllm": VLLMProvider(),
        "sglang": SGLangProvider(),
        "deepseek": DeepSeekProvider(DeepSeekProviderOptions(thinking="disabled")),
    }
    payload = providers[provider_key]._build_payload(req)
    assert payload["tool_choice"] == {"type": "function", "function": {"name": "Weather"}}


async def test_gemini_tool_choice_mapping_modes() -> None:
    from linch.providers.gemini import _translate_tool_choice

    assert _translate_tool_choice(None) is None
    assert _translate_tool_choice("auto") == {"function_calling_config": {"mode": "AUTO"}}
    assert _translate_tool_choice("none") == {"function_calling_config": {"mode": "NONE"}}
    assert _translate_tool_choice("required") == {"function_calling_config": {"mode": "ANY"}}
    assert _translate_tool_choice({"name": "X"}) == {
        "function_calling_config": {"mode": "ANY", "allowed_function_names": ["X"]}
    }


async def test_gemini_omits_tool_config_when_no_tools() -> None:
    from linch.providers.gemini import GeminiProvider

    sdk = pytest.importorskip("google.genai")

    async def empty_stream() -> Any:
        if False:
            yield

    generate = AsyncMock(return_value=empty_stream())
    fake_client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content_stream=generate))
    )
    req = ProviderRequest(
        model="m",
        system=[],
        tools=[],
        messages=[Message(role="user", content=[TextBlock(text="hi")])],
        tool_choice="required",
    )
    with patch.object(sdk, "Client", return_value=fake_client):
        _ = [e async for e in GeminiProvider().stream(req)]

    assert generate.call_args.kwargs["config"].tool_config is None


# ── transport closers (Anthropic + OpenAI Responses) ─────────────────────────


class _FakeAsyncClient:
    def __init__(self, closed: list[str]) -> None:
        self._closed = closed

    async def aclose(self) -> None:
        self._closed.append("closed")


class _FakeDualCloseClient:
    def __init__(self, closed: list[str]) -> None:
        self._closed = closed

    async def aclose(self) -> None:
        self._closed.append("async")

    def close(self) -> None:
        self._closed.append("sync")


@pytest.mark.parametrize("client_kind,expected", [("async", "async"), ("sync", "sync")])
async def test_aclose_client_supports_async_and_sync_close(client_kind: str, expected: str) -> None:
    from linch._client_lifecycle import aclose_client

    closed: list[str] = []
    client = (
        _FakeDualCloseClient(closed)
        if client_kind == "async"
        else type("SyncClient", (), {"close": lambda self: closed.append("sync")})()
    )
    await aclose_client(client)

    assert closed == [expected]


async def test_anthropic_aclose_closes_client_and_is_idempotent() -> None:
    from linch.providers.anthropic import AnthropicProvider

    provider = AnthropicProvider()
    closed: list[str] = []
    provider._client = _FakeAsyncClient(closed)

    await provider.aclose()
    assert closed == ["closed"]
    assert provider._client is None

    await provider.aclose()  # idempotent — no client, no raise
    assert closed == ["closed"]


async def test_openai_responses_aclose_closes_underlying_client_and_is_idempotent() -> None:
    from linch.providers.openai_responses import OpenAIResponsesProvider

    provider = OpenAIResponsesProvider()
    closed: list[str] = []
    # The provider's _client is the Linch wrapper; the SDK client lives on .client.
    provider._client.client = _FakeAsyncClient(closed)

    await provider.aclose()
    assert closed == ["closed"]
    assert provider._client.client is None

    await provider.aclose()  # idempotent
    assert closed == ["closed"]
