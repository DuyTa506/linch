from __future__ import annotations


def test_catalog_lists_direct_provider_records() -> None:
    from linch.providers import list_provider_models

    records = list_provider_models()
    provider_ids = {record.provider_id for record in records}

    assert "anthropic" in provider_ids
    assert "openai-responses" in provider_ids
    assert "openai-chat" in provider_ids
    assert "gemini" in provider_ids
    assert "llamacpp" not in provider_ids
    assert "sglang" not in provider_ids
    assert "vllm" not in provider_ids
    assert "deepseek" not in provider_ids


def test_local_server_providers_are_importable() -> None:
    import linch
    from linch.providers import (
        SGLangProvider,
        SGLangProviderOptions,
        VLLMProvider,
        VLLMProviderOptions,
    )

    assert linch.SGLangProvider is SGLangProvider
    assert linch.SGLangProviderOptions is SGLangProviderOptions
    assert linch.VLLMProvider is VLLMProvider
    assert linch.VLLMProviderOptions is VLLMProviderOptions


def test_catalog_filters_by_provider_id() -> None:
    from linch.providers import list_provider_models

    anthropic_records = list_provider_models("anthropic")

    assert anthropic_records
    assert all(record.provider_id == "anthropic" for record in anthropic_records)
    assert list_provider_models("no-such-provider") == []


def test_get_provider_model_info_exact_match() -> None:
    from linch.providers import get_provider_model_info

    info = get_provider_model_info("claude-sonnet-4-6", provider_id="anthropic")

    assert info is not None
    assert info.provider_id == "anthropic"
    assert info.model == "claude-sonnet-4-6"
    assert get_provider_model_info("claude-sonnet-4") is None
    assert get_provider_model_info("claude-sonnet-4-6", provider_id="gemini") is None


def test_catalog_records_use_provider_capabilities() -> None:
    from linch.providers import ProviderCapabilities, get_provider_model_info
    from linch.providers.anthropic import AnthropicProvider
    from linch.providers.gemini import GeminiProvider
    from linch.providers.openai_chat import OpenAIChatCompletionsProvider
    from linch.providers.openai_responses import OpenAIResponsesProvider

    cases = [
        ("anthropic", "claude-opus-4-8", AnthropicProvider()),
        ("openai-responses", "gpt-5", OpenAIResponsesProvider()),
        ("openai-chat", "gpt-4o", OpenAIChatCompletionsProvider()),
        ("gemini", "gemini-2.5-pro", GeminiProvider()),
    ]

    for provider_id, model, provider in cases:
        info = get_provider_model_info(model, provider_id=provider_id)
        assert info is not None
        assert isinstance(info.capabilities, ProviderCapabilities)
        assert info.context_window == provider.context_window(model)
        assert info.capabilities == provider.capabilities(model)


def test_catalog_pricing_is_conservative() -> None:
    from linch.pricing import ModelPricing
    from linch.providers import get_provider_model_info

    anthropic = get_provider_model_info("claude-sonnet-4-6", provider_id="anthropic")
    openai = get_provider_model_info("gpt-4o", provider_id="openai-chat")
    gemini = get_provider_model_info("gemini-2.5-pro", provider_id="gemini")

    assert anthropic is not None
    assert isinstance(anthropic.pricing, ModelPricing)
    assert anthropic.pricing_source == "https://www.anthropic.com/pricing"

    assert openai is not None
    assert openai.pricing is None
    assert openai.pricing_source is None

    assert gemini is not None
    assert gemini.pricing is None
    assert gemini.pricing_source is None


def test_catalog_helpers_exported_from_root_package() -> None:
    import linch

    assert linch.get_provider_model_info("claude-sonnet-4-6", "anthropic") is not None
    assert linch.list_provider_models("gemini")
