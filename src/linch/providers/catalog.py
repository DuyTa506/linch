from __future__ import annotations

from dataclasses import dataclass

from linch.pricing import _DEFAULT_PRICING, ModelPricing

from .anthropic import _KNOWN_CONTEXT as _ANTHROPIC_CONTEXT
from .anthropic import AnthropicProvider
from .base import ProviderCapabilities
from .gemini import _KNOWN_CONTEXT as _GEMINI_CONTEXT
from .gemini import GeminiProvider
from .openai_chat import _KNOWN_CONTEXT as _OPENAI_CHAT_CONTEXT
from .openai_chat import OpenAIChatCompletionsProvider
from .openai_responses import OpenAIResponsesProvider

_ANTHROPIC_PRICING_SOURCE = "https://www.anthropic.com/pricing"


@dataclass(frozen=True, slots=True)
class ProviderModelInfo:
    provider_id: str
    model: str
    display_name: str
    context_window: int
    capabilities: ProviderCapabilities
    pricing: ModelPricing | None
    pricing_source: str | None
    notes: str = ""


def _display_name(model: str) -> str:
    return model.replace("-", " ").title()


def _anthropic_models() -> list[ProviderModelInfo]:
    provider = AnthropicProvider()
    records = []
    for model in sorted(_ANTHROPIC_CONTEXT):
        pricing = _DEFAULT_PRICING.get(model)
        records.append(
            ProviderModelInfo(
                provider_id=provider.id,
                model=model,
                display_name=_display_name(model),
                context_window=provider.context_window(model),
                capabilities=provider.capabilities(model),
                pricing=pricing,
                pricing_source=_ANTHROPIC_PRICING_SOURCE if pricing is not None else None,
                notes="Direct Anthropic Messages provider with prompt caching support.",
            )
        )
    return records


def _openai_responses_models() -> list[ProviderModelInfo]:
    from linch.openai_responses import KNOWN_CONTEXT_WINDOWS

    provider = OpenAIResponsesProvider()
    return [
        ProviderModelInfo(
            provider_id=provider.id,
            model=model,
            display_name=_display_name(model),
            context_window=provider.context_window(model),
            capabilities=provider.capabilities(model),
            pricing=None,
            pricing_source=None,
            notes="Direct OpenAI Responses provider with native reasoning controls.",
        )
        for model in sorted(KNOWN_CONTEXT_WINDOWS)
    ]


def _openai_chat_models() -> list[ProviderModelInfo]:
    provider = OpenAIChatCompletionsProvider()
    return [
        ProviderModelInfo(
            provider_id=provider.id,
            model=model,
            display_name=_display_name(model),
            context_window=provider.context_window(model),
            capabilities=provider.capabilities(model),
            pricing=None,
            pricing_source=None,
            notes="OpenAI Chat Completions provider; also works with compatible endpoints.",
        )
        for model in sorted(_OPENAI_CHAT_CONTEXT)
    ]


def _gemini_models() -> list[ProviderModelInfo]:
    provider = GeminiProvider()
    return [
        ProviderModelInfo(
            provider_id=provider.id,
            model=model,
            display_name=_display_name(model),
            context_window=provider.context_window(model),
            capabilities=provider.capabilities(model),
            pricing=None,
            pricing_source=None,
            notes="Direct Google Gemini provider.",
        )
        for model in sorted(_GEMINI_CONTEXT)
    ]


def _catalog() -> list[ProviderModelInfo]:
    return [
        *_anthropic_models(),
        *_openai_responses_models(),
        *_openai_chat_models(),
        *_gemini_models(),
    ]


def list_provider_models(provider_id: str | None = None) -> list[ProviderModelInfo]:
    """Return known static model metadata for built-in direct providers.

    The catalog intentionally excludes OpenAI-compatible services and local
    llama.cpp models whose model lists depend on external configuration.
    """
    models = _catalog()
    if provider_id is None:
        return models
    return [info for info in models if info.provider_id == provider_id]


def get_provider_model_info(
    model: str,
    provider_id: str | None = None,
) -> ProviderModelInfo | None:
    """Return catalog metadata for an exact model id, or ``None`` if unknown."""
    for info in list_provider_models(provider_id):
        if info.model == model:
            return info
    return None
