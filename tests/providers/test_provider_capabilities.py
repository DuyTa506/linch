"""Tests for the provider capabilities subsystem (Phase 7).

Unit tests cover ProviderCapabilities defaults, per-provider declarations,
and the apply_provider_capabilities downgrade helper.
"""

from __future__ import annotations

import pytest

from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn

# ---------------------------------------------------------------------------
# ProviderCapabilities dataclass defaults
# ---------------------------------------------------------------------------


def test_capabilities_defaults():
    from linch.providers import ProviderCapabilities

    caps = ProviderCapabilities()
    assert caps.context_window == 128_000
    assert caps.parallel_tool_calls is True
    assert caps.structured_output is True
    assert caps.structured_output_terminal_tool is False
    assert caps.tool_choice is True
    assert caps.prompt_cache is False
    assert caps.can_disable_thinking is False


def test_capabilities_custom_values():
    from linch.providers import ProviderCapabilities

    caps = ProviderCapabilities(
        context_window=200_000,
        prompt_cache=True,
        structured_output=False,
    )
    assert caps.context_window == 200_000
    assert caps.prompt_cache is True
    assert caps.structured_output is False


# ---------------------------------------------------------------------------
# Per-provider capability declarations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "provider_key,model,expected_window,minimum_window,terminal,expected_id,expected_parallel,can_disable_thinking",
    [
        ("openai-chat", "gpt-4o", 128_000, 0, False, "openai-chat", True, False),
        ("openai-responses", "gpt-5", None, 1, False, "openai-responses", True, False),
        ("vllm", "served-model", 65_536, 0, False, "vllm", False, False),
        ("sglang", "served-model", 65_536, 0, False, "sglang", False, False),
        ("anthropic", "claude-opus-4-8", 200_000, 0, False, "anthropic", True, True),
        ("anthropic-compatible", "deepseek-v4-flash", 200_000, 0, True, "anthropic", True, True),
        ("deepseek", "deepseek-v4-flash", None, 1, False, "deepseek", True, True),
        ("llamacpp", "local-model", 128_000, 0, False, "llamacpp", True, True),
        ("gemini-flash", "gemini-2.0-flash", None, 1_000_000, False, "gemini", True, False),
        ("gemini-pro", "gemini-2.5-pro", None, 1_000_000, False, "gemini", True, False),
        ("gemini-15", "gemini-1.5-pro", 2_000_000, 0, False, "gemini", True, False),
        ("gemini-unknown", "gemini-unknown-xyz", None, 32_000, False, "gemini", True, False),
    ],
)
def test_provider_capability_flags(
    provider_key: str,
    model: str,
    expected_window: int | None,
    minimum_window: int,
    terminal: bool,
    expected_id: str,
    expected_parallel: bool,
    can_disable_thinking: bool,
) -> None:
    from linch.providers import (
        AnthropicProvider,
        AnthropicProviderOptions,
        DeepSeekProvider,
        GeminiProvider,
        LlamaCppProvider,
        OpenAIChatCompletionsProvider,
        OpenAIResponsesProvider,
        SGLangProvider,
        SGLangProviderOptions,
        VLLMProvider,
        VLLMProviderOptions,
    )

    providers = {
        "openai-chat": OpenAIChatCompletionsProvider(),
        "openai-responses": OpenAIResponsesProvider(),
        "vllm": VLLMProvider(VLLMProviderOptions(context_window=65_536, parallel_tool_calls=False)),
        "sglang": SGLangProvider(
            SGLangProviderOptions(context_window=65_536, parallel_tool_calls=False)
        ),
        "anthropic": AnthropicProvider(),
        "anthropic-compatible": AnthropicProvider(
            AnthropicProviderOptions(base_url="https://api.deepseek.com/anthropic")
        ),
        "deepseek": DeepSeekProvider(),
        "llamacpp": LlamaCppProvider(),
        "gemini-flash": GeminiProvider(),
        "gemini-pro": GeminiProvider(),
        "gemini-15": GeminiProvider(),
        "gemini-unknown": GeminiProvider(),
    }
    caps = providers[provider_key].capabilities(model)

    if expected_window is None:
        assert caps.context_window >= minimum_window
    else:
        assert caps.context_window == expected_window
    assert providers[provider_key].id == expected_id
    assert caps.parallel_tool_calls is expected_parallel
    assert caps.structured_output is True
    assert caps.tool_choice is True
    assert caps.prompt_cache is True
    assert caps.structured_output_terminal_tool is terminal
    assert caps.can_disable_thinking is can_disable_thinking


def test_base_provider_default_capabilities():
    """BaseProvider.capabilities() default derives context_window from context_window()."""
    from linch.providers import BaseProvider, ProviderCapabilities

    class _MinimalProvider(BaseProvider):
        id = "minimal"

        def context_window(self, model):
            return 99_999

        async def stream(self, req):
            if False:
                yield {}

    p = _MinimalProvider()
    caps = p.capabilities("any-model")
    assert isinstance(caps, ProviderCapabilities)
    assert caps.context_window == 99_999
    # Defaults for unspecified flags
    assert caps.prompt_cache is False
    assert caps.structured_output is True


# ---------------------------------------------------------------------------
# apply_provider_capabilities downgrade helper
# ---------------------------------------------------------------------------


def _make_req(**overrides):
    """Build a minimal ProviderRequest with sensible defaults for testing."""
    from linch.types import OutputSchema, ProviderRequest

    defaults = dict(
        model="test-model",
        system=[],
        tools=[],
        messages=[],
        cache_prompt=True,
        cache_ttl="5m",
        tool_choice="auto",
        output_schema=OutputSchema(name="out", schema={"type": "object"}),
    )
    defaults.update(overrides)
    return ProviderRequest(**defaults)


@pytest.mark.parametrize(
    "caps_kwargs,expected",
    [
        ({"prompt_cache": False}, {"cache_prompt": None, "cache_ttl": None}),
        ({"prompt_cache": True}, {"cache_prompt": True, "cache_ttl": "5m"}),
        ({"tool_choice": False}, {"tool_choice": None}),
        ({"structured_output": False}, {"output_schema": None}),
    ],
)
def test_apply_provider_capability_downgrades(caps_kwargs: dict, expected: dict):
    from linch.loop import apply_provider_capabilities
    from linch.providers import ProviderCapabilities

    req = _make_req()
    assert req.cache_prompt is True
    assert req.cache_ttl == "5m"
    assert req.tool_choice == "auto"
    assert req.output_schema is not None
    apply_provider_capabilities(req, ProviderCapabilities(**caps_kwargs))
    for attr, value in expected.items():
        assert getattr(req, attr) == value


def test_apply_preserves_cache_for_openai_chat():
    """OpenAI Chat uses provider-native automatic prompt caching."""
    from linch.loop import apply_provider_capabilities
    from linch.providers import OpenAIChatCompletionsProvider

    provider = OpenAIChatCompletionsProvider()
    caps = provider.capabilities("gpt-4o")
    req = _make_req()

    apply_provider_capabilities(req, caps)

    assert req.cache_prompt is True
    assert req.cache_ttl == "5m"
    # Structured output supported → preserved
    assert req.output_schema is not None
    # Tool choice supported → preserved
    assert req.tool_choice == "auto"


def test_apply_no_downgrade_for_anthropic():
    """Anthropic caps: cache preserved, structured_output preserved (Feature A)."""
    from linch.loop import apply_provider_capabilities
    from linch.providers import AnthropicProvider

    provider = AnthropicProvider()
    caps = provider.capabilities("claude-opus-4-8")
    req = _make_req()

    apply_provider_capabilities(req, caps)

    # Prompt cache supported → preserved
    assert req.cache_prompt is True
    assert req.cache_ttl == "5m"
    # Structured output now supported (forced-tool method) → preserved
    assert req.output_schema is not None
    # Tool choice supported → preserved
    assert req.tool_choice == "auto"


# ---------------------------------------------------------------------------
# Integration: _build_turn_request applies downgrades via capabilities
# ---------------------------------------------------------------------------


class CapRecordingProvider(ScriptedProvider):
    """Scripted provider that advertises unsupported request features."""

    def context_window(self, model: str) -> int:
        return 128_000

    def capabilities(self, model: str):
        from linch.providers import ProviderCapabilities

        # Declare: no prompt_cache, no structured_output
        return ProviderCapabilities(
            context_window=128_000,
            prompt_cache=False,
            structured_output=False,
            tool_choice=True,
        )


@pytest.mark.asyncio
async def test_build_turn_request_applies_capability_downgrade():
    """Run a full session and verify the provider received a downgraded request."""
    from linch.types import OutputSchema

    provider = CapRecordingProvider([TextTurn("result")])
    agent = make_agent(
        provider,
        output_schema=OutputSchema(name="out", schema={"type": "object"}),
        loop_guard=None,
    )
    session = await agent.session()
    async for _ in session.run("hello"):
        pass

    assert provider.requests, "provider.stream was never called"
    req = provider.requests[-1]

    # Capability downgrade applied: prompt_cache=False → cleared
    assert req.cache_prompt is None, "cache_prompt should be cleared for non-caching provider"
    assert req.cache_ttl is None, "cache_ttl should be cleared for non-caching provider"

    # structured_output=False → output_schema cleared on req (loop text-parses instead)
    assert req.output_schema is None, (
        "output_schema should be cleared when provider lacks structured output"
    )
