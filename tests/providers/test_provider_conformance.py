"""Provider conformance suite (ROADMAP Phase 3.1).

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

import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from linch.testing import assert_provider_contract
from linch.types import Message, ProviderRequest, TextBlock

pytestmark = pytest.mark.asyncio


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


def _req_forcing(name: str) -> ProviderRequest:
    return ProviderRequest(
        model="m",
        system=[],
        tools=[
            {
                "name": name,
                "description": "d",
                "input_schema": {"type": "object", "properties": {}},
            }
        ],
        messages=[Message(role="user", content=[TextBlock(text="hi")])],
        tool_choice={"name": name},
    )


@pytest.mark.parametrize("name,cls,model", _all_providers(), ids=[p[0] for p in _all_providers()])
async def test_provider_satisfies_lifecycle_contract(name: str, cls: Any, model: str) -> None:
    await assert_provider_contract(lambda: cls(), model=model)


# ── tool_choice must be honored wherever it is advertised ────────────────────


async def test_anthropic_builder_maps_forced_tool_choice() -> None:
    from linch.providers.anthropic import AnthropicProviderOptions, _build_payload

    payload = _build_payload(_req_forcing("Weather"), AnthropicProviderOptions())
    assert payload["tool_choice"] == {"type": "tool", "name": "Weather"}


async def test_openai_responses_builder_maps_forced_tool_choice() -> None:
    from linch.openai_responses import build_payload

    payload = build_payload(_req_forcing("Weather"))
    assert payload["tool_choice"] == {"type": "function", "name": "Weather"}


@pytest.mark.parametrize(
    "cls",
    [
        "OpenAIChatCompletionsProvider",
        "LlamaCppProvider",
        "VLLMProvider",
        "SGLangProvider",
        "DeepSeekProvider",
    ],
)
async def test_openai_compatible_builders_map_forced_tool_choice(cls: str) -> None:
    import linch.providers.deepseek as deepseek
    import linch.providers.llamacpp as llamacpp
    import linch.providers.openai_chat as openai_chat
    import linch.providers.sglang as sglang
    import linch.providers.vllm as vllm

    lookup = {
        "OpenAIChatCompletionsProvider": openai_chat.OpenAIChatCompletionsProvider,
        "LlamaCppProvider": llamacpp.LlamaCppProvider,
        "VLLMProvider": vllm.VLLMProvider,
        "SGLangProvider": sglang.SGLangProvider,
        "DeepSeekProvider": deepseek.DeepSeekProvider,
    }
    if cls == "DeepSeekProvider":
        provider = lookup[cls](deepseek.DeepSeekProviderOptions(thinking="disabled"))
    else:
        provider = lookup[cls]()
    payload = provider._build_payload(_req_forcing("Weather"))
    assert payload["tool_choice"] == {"type": "function", "function": {"name": "Weather"}}


async def test_gemini_stream_maps_forced_tool_choice_to_tool_config() -> None:
    from linch.providers.gemini import GeminiProvider

    fake_candidate = MagicMock()
    fake_candidate.content.parts = []
    fake_candidate.finish_reason = 1  # STOP

    fake_chunk = MagicMock()
    fake_chunk.candidates = [fake_candidate]
    fake_chunk.usage_metadata.prompt_token_count = 1
    fake_chunk.usage_metadata.candidates_token_count = 1

    async def _fake_stream(*args: Any, **kwargs: Any) -> Any:
        yield fake_chunk

    fake_model = MagicMock()
    fake_model.generate_content_async = MagicMock(return_value=_fake_stream())
    fake_genai = MagicMock()
    fake_genai.GenerativeModel.return_value = fake_model

    with patch.dict(sys.modules, {"google.generativeai": fake_genai}):
        provider = GeminiProvider()
        _ = [e async for e in provider.stream(_req_forcing("Weather"))]

    kwargs = fake_genai.GenerativeModel.call_args.kwargs
    assert kwargs["tool_config"] == {
        "function_calling_config": {"mode": "ANY", "allowed_function_names": ["Weather"]}
    }


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

    fake_candidate = MagicMock()
    fake_candidate.content.parts = []
    fake_candidate.finish_reason = 1

    fake_chunk = MagicMock()
    fake_chunk.candidates = [fake_candidate]
    fake_chunk.usage_metadata.prompt_token_count = 1
    fake_chunk.usage_metadata.candidates_token_count = 1

    async def _fake_stream(*args: Any, **kwargs: Any) -> Any:
        yield fake_chunk

    fake_model = MagicMock()
    fake_model.generate_content_async = MagicMock(return_value=_fake_stream())
    fake_genai = MagicMock()
    fake_genai.GenerativeModel.return_value = fake_model

    req = ProviderRequest(
        model="m",
        system=[],
        tools=[],
        messages=[Message(role="user", content=[TextBlock(text="hi")])],
        tool_choice="required",  # ignored: no tools to force
    )
    with patch.dict(sys.modules, {"google.generativeai": fake_genai}):
        _ = [e async for e in GeminiProvider().stream(req)]

    assert "tool_config" not in fake_genai.GenerativeModel.call_args.kwargs


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


async def test_aclose_client_prefers_async_close() -> None:
    from linch._client_lifecycle import aclose_client

    closed: list[str] = []
    await aclose_client(_FakeDualCloseClient(closed))

    assert closed == ["async"]


async def test_aclose_client_supports_sync_close() -> None:
    from linch._client_lifecycle import aclose_client

    closed: list[str] = []
    await aclose_client(type("SyncClient", (), {"close": lambda self: closed.append("sync")})())

    assert closed == ["sync"]


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
