"""Built-in provider construction, intentionally reached only on AI requests."""

from __future__ import annotations

from typing import Any, cast

from .config import AuthoringConfig


def _openai_reasoning(config: AuthoringConfig) -> Any:
    """Reasoning options for the Responses API; summaries surface as thinking."""

    if config.reasoning == "off":
        return None
    from linch import OpenAIReasoning

    return OpenAIReasoning(effort=config.reasoning, summary="auto")  # type: ignore[arg-type]


def _chat_options(config: AuthoringConfig) -> Any:
    from linch import OpenAIChatProviderOptions

    return OpenAIChatProviderOptions(
        api_key=config.api_key,
        base_url=config.base_url,
        context_window=config.context_window,
        json_mode=config.json_mode,
    )


def _anthropic_thinking(config: AuthoringConfig) -> dict[str, Any] | None:
    if config.reasoning == "off":
        # Omitting ``thinking`` means "use the model default". Some modern
        # Claude routes enable adaptive thinking by default, so Studio's
        # explicit "off" must travel as the native disabled mode.
        return {"type": "disabled"}
    # Current Claude models use adaptive thinking plus output_config.effort.
    # Manual enabled/budget_tokens is rejected by the newest Claude models and
    # deprecated on the preceding generation.
    return {"type": "adaptive"}


def _deepseek_options(config: AuthoringConfig) -> Any:
    """Build the native DeepSeek profile, not the Anthropic shim."""

    from linch import DeepSeekProviderOptions

    return DeepSeekProviderOptions(
        api_key=config.api_key,
        base_url=config.base_url,
        context_window=config.context_window,
        thinking="disabled" if config.reasoning == "off" else "enabled",
        effort=None if config.reasoning == "off" else cast(Any, config.reasoning),
    )


def _uses_native_deepseek(config: AuthoringConfig) -> bool:
    """Keep established OpenAI-chat DeepSeek configs on the correct wire mode."""

    return config.provider == "deepseek" or (
        config.provider == "openai_chat"
        and config.base_url is not None
        and config.model.lower().startswith("deepseek-")
    )


def create_authoring_provider(config: AuthoringConfig) -> Any:
    """Create a Linch provider exclusively from ``LINCH_STUDIO_*`` config."""

    if config.provider == "openai_responses":
        from linch import OpenAIResponsesProvider, OpenAIResponsesProviderOptions

        return OpenAIResponsesProvider(
            OpenAIResponsesProviderOptions(
                api_key=config.api_key,
                base_url=config.base_url,
                reasoning=_openai_reasoning(config),
            )
        )
    if _uses_native_deepseek(config):
        from linch import DeepSeekProvider

        return DeepSeekProvider(_deepseek_options(config))
    if config.provider == "openai_chat":
        from linch import OpenAIChatCompletionsProvider

        return OpenAIChatCompletionsProvider(_chat_options(config))
    if config.provider == "anthropic":
        from linch import AnthropicProvider, AnthropicProviderOptions

        return AnthropicProvider(
            AnthropicProviderOptions(
                api_key=config.api_key,
                base_url=config.base_url,
                thinking=_anthropic_thinking(config),
                api_mode="native",
                effort=None if config.reasoning == "off" else config.reasoning,
            )
        )
    if config.provider == "gemini":
        from linch import GeminiProvider, GeminiProviderOptions

        return GeminiProvider(
            GeminiProviderOptions(
                api_key=config.api_key,
                project=config.project,
                location=config.location,
            )
        )

    # OpenAI-compatible local servers must receive an explicit base URL. A
    # non-secret placeholder prevents their SDK from consulting OPENAI_API_KEY.
    local_key = config.api_key or "linch-studio-local"
    context_window = config.context_window or 128_000
    if config.provider == "llama_cpp":
        from linch import LlamaCppProvider, LlamaCppProviderOptions

        return LlamaCppProvider(
            LlamaCppProviderOptions(
                api_key=local_key,
                base_url=config.base_url,
                context_window=context_window,
            )
        )
    if config.provider == "vllm":
        from linch import VLLMProvider, VLLMProviderOptions

        return VLLMProvider(
            VLLMProviderOptions(
                api_key=local_key,
                base_url=config.base_url,
                context_window=context_window,
            )
        )
    if config.provider == "sglang":
        from linch import SGLangProvider, SGLangProviderOptions

        return SGLangProvider(
            SGLangProviderOptions(
                api_key=local_key,
                base_url=config.base_url,
                context_window=context_window,
            )
        )
    raise AssertionError("AuthoringConfig accepted an unhandled provider")


__all__ = ["create_authoring_provider"]
