"""Tests for the provider capabilities subsystem (Phase 7).

Unit tests cover ProviderCapabilities defaults, per-provider declarations,
and the apply_provider_capabilities downgrade helper.
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# ProviderCapabilities dataclass defaults
# ---------------------------------------------------------------------------


def test_capabilities_defaults():
    from linch.providers import ProviderCapabilities

    caps = ProviderCapabilities()
    assert caps.context_window == 128_000
    assert caps.parallel_tool_calls is True
    assert caps.structured_output is True
    assert caps.tool_choice is True
    assert caps.prompt_cache is False


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


def test_openai_chat_capabilities():
    from linch.providers import OpenAIChatCompletionsProvider

    provider = OpenAIChatCompletionsProvider()
    caps = provider.capabilities("gpt-4o")

    assert caps.context_window == 128_000
    assert caps.structured_output is True
    assert caps.tool_choice is True
    assert caps.prompt_cache is True


def test_openai_responses_capabilities():
    from linch.providers import OpenAIResponsesProvider

    provider = OpenAIResponsesProvider()
    caps = provider.capabilities("gpt-5")

    assert caps.context_window > 0
    assert caps.structured_output is True
    assert caps.tool_choice is True
    assert caps.prompt_cache is True


def test_vllm_capabilities():
    from linch.providers import VLLMProvider, VLLMProviderOptions

    provider = VLLMProvider(VLLMProviderOptions(context_window=65_536))
    caps = provider.capabilities("served-model")

    assert caps.context_window == 65_536
    assert caps.structured_output is True
    assert caps.tool_choice is True
    assert caps.prompt_cache is True


def test_sglang_capabilities():
    from linch.providers import SGLangProvider, SGLangProviderOptions

    provider = SGLangProvider(SGLangProviderOptions(context_window=65_536))
    caps = provider.capabilities("served-model")

    assert caps.context_window == 65_536
    assert caps.structured_output is True
    assert caps.tool_choice is True
    assert caps.prompt_cache is True


def test_anthropic_capabilities():
    from linch.providers import AnthropicProvider

    provider = AnthropicProvider()
    caps = provider.capabilities("claude-opus-4-8")

    assert caps.context_window == 200_000
    assert caps.prompt_cache is True
    assert caps.structured_output is True  # forced-tool method (Feature A)
    assert caps.tool_choice is True


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


def test_apply_clears_cache_when_not_supported():
    from linch.loop import apply_provider_capabilities
    from linch.providers import ProviderCapabilities

    req = _make_req()
    assert req.cache_prompt is True
    assert req.cache_ttl == "5m"

    caps = ProviderCapabilities(prompt_cache=False)
    apply_provider_capabilities(req, caps)

    assert req.cache_prompt is None
    assert req.cache_ttl is None


def test_apply_preserves_cache_when_supported():
    from linch.loop import apply_provider_capabilities
    from linch.providers import ProviderCapabilities

    req = _make_req()
    caps = ProviderCapabilities(prompt_cache=True)
    apply_provider_capabilities(req, caps)

    assert req.cache_prompt is True
    assert req.cache_ttl == "5m"


def test_apply_clears_tool_choice_when_not_supported():
    from linch.loop import apply_provider_capabilities
    from linch.providers import ProviderCapabilities

    req = _make_req()
    assert req.tool_choice == "auto"

    caps = ProviderCapabilities(tool_choice=False)
    apply_provider_capabilities(req, caps)

    assert req.tool_choice is None


def test_apply_clears_output_schema_when_not_supported():
    from linch.loop import apply_provider_capabilities
    from linch.providers import ProviderCapabilities

    req = _make_req()
    assert req.output_schema is not None

    caps = ProviderCapabilities(structured_output=False)
    apply_provider_capabilities(req, caps)

    assert req.output_schema is None


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


class CapRecordingProvider:
    """Fake provider that records requests to verify capability downgrade."""

    id = "cap-recording"
    received_req = None

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

    async def stream(self, req):
        from linch.types import Usage

        CapRecordingProvider.received_req = req
        yield {"type": "message_start", "model": req.model}
        yield {"type": "text_delta", "text": "result"}
        yield {
            "type": "message_end",
            "stop_reason": "end_turn",
            "usage": Usage(),
            "provider_metadata": None,
        }


@pytest.mark.asyncio
async def test_build_turn_request_applies_capability_downgrade():
    """Run a full session and verify the provider received a downgraded request."""
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools
    from linch.types import OutputSchema

    CapRecordingProvider.received_req = None
    provider = CapRecordingProvider()

    agent = Agent(
        model="test-model",
        provider=provider,
        tools=empty_tools(),
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        output_schema=OutputSchema(name="out", schema={"type": "object"}),
        loop_guard=None,
    )
    session = await agent.session()
    async for _ in session.run("hello"):
        pass

    req = CapRecordingProvider.received_req
    assert req is not None, "provider.stream was never called"

    # Capability downgrade applied: prompt_cache=False → cleared
    assert req.cache_prompt is None, "cache_prompt should be cleared for non-caching provider"
    assert req.cache_ttl is None, "cache_ttl should be cleared for non-caching provider"

    # structured_output=False → output_schema cleared on req (loop text-parses instead)
    assert req.output_schema is None, (
        "output_schema should be cleared when provider lacks structured output"
    )
