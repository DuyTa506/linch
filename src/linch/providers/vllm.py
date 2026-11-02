from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from linch.providers._context_probe import probe_context_windows, probed_context_window
from linch.providers.base import ProviderCapabilities
from linch.providers.openai_chat import (
    OpenAIChatProviderOptions,
    _build_openai_compatible_payload,
    _ChatCompletionsProvider,
)
from linch.types import ModelId, ProviderRequest, Usage

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class VLLMProviderOptions:
    """Configure the vLLM OpenAI-compatible endpoint.

    Args:
        api_key: Optional endpoint credential.
        base_url: vLLM server endpoint.
        default_headers: Headers added to requests.
        context_window: Optional server context capacity in tokens.
        auto_context_window: Whether ``prepare`` probes ``/v1/models``.
        context_window_timeout: Timeout for the optional models probe in seconds.
        json_mode: Whether to request JSON-object output.
        parallel_tool_calls: Optional parallel-call support declaration.
        chat_template_kwargs: Model-specific chat-template settings.
        disable_thinking_kwargs: Model-specific settings used for disabled thinking.
        extra_body: Additional request fields merged into the payload.

    Attributes:
        json_mode: Requests JSON-object transport for structured responses.
        chat_template_kwargs: Model-specific chat-template settings.
        disable_thinking_kwargs: Model-specific disabled-thinking settings.
        extra_body: Provider-specific fields merged into each request payload.
    """

    api_key: str | None = None
    base_url: str | None = None
    default_headers: dict[str, str] | None = None
    context_window: int | None = None
    json_mode: bool = False
    parallel_tool_calls: bool | None = None
    extra_body: dict[str, Any] | None = None
    auto_context_window: bool = True
    context_window_timeout: float = 2.0
    chat_template_kwargs: dict[str, Any] | None = None
    disable_thinking_kwargs: dict[str, Any] | None = None


class VLLMProvider(_ChatCompletionsProvider):
    """vLLM provider using its OpenAI-compatible chat completions endpoint.

    Args:
        options: Optional vLLM endpoint and request settings.
    """

    id = "vllm"

    def __init__(self, options: VLLMProviderOptions | None = None) -> None:
        """Create a vLLM provider with optional server settings.

        Args:
            options: vLLM endpoint and request options.
        """
        opts = options or VLLMProviderOptions()
        self._vllm_options = opts
        self._context_window_cache: dict[str, int] | None = None
        super().__init__(
            OpenAIChatProviderOptions(
                api_key=opts.api_key,
                base_url=opts.base_url,
                default_headers=opts.default_headers,
                context_window=opts.context_window,
                parallel_tool_calls=opts.parallel_tool_calls,
                extra_body=opts.extra_body,
                json_mode=opts.json_mode,
            )
        )

    def _build_payload(self, req: ProviderRequest) -> dict[str, Any]:
        return _build_vllm_payload(req, self._vllm_options)

    def context_window(self, model: ModelId) -> int:
        """Return configured or probed vLLM context capacity.

        Args:
            model: Served model identifier.

        Returns:
            The configured or discovered positive token capacity.
        """
        configured = self._vllm_options.context_window
        if isinstance(configured, int) and not isinstance(configured, bool) and configured > 0:
            return configured
        return probed_context_window(self._context_window_cache, model)

    async def prepare(self) -> None:
        """Probe vLLM's model catalog once when enabled."""
        opts = self._vllm_options
        if self._context_window_cache is not None:
            return
        if opts.context_window is not None or not opts.auto_context_window:
            self._context_window_cache = {}
            return
        self._context_window_cache = await probe_context_windows(
            self._get_client,
            base_url=opts.base_url,
            timeout=opts.context_window_timeout,
        )

    def capabilities(self, model: ModelId) -> ProviderCapabilities:
        """Return capabilities declared for the vLLM adapter.

        Args:
            model: Served model identifier.

        Returns:
            vLLM capability flags and effective context capacity.
        """
        return ProviderCapabilities(
            context_window=self.context_window(model),
            parallel_tool_calls=self._vllm_options.parallel_tool_calls is not False,
            structured_output=True,
            tool_choice=True,
            prompt_cache=True,
            can_disable_thinking=self._vllm_options.disable_thinking_kwargs is not None,
        )

    def _reasoning_text(self, delta: Any) -> str | None:
        value = getattr(delta, "reasoning", None) or getattr(delta, "reasoning_content", None)
        return str(value) if value else None

    def _usage_from_chunk(self, chunk_usage: Any) -> Usage:
        details = getattr(chunk_usage, "prompt_tokens_details", None)
        if details is None and not getattr(self, "_vllm_cache_warning_emitted", False):
            _LOGGER.warning(
                "vLLM prompt cache token details are unavailable; start the server with "
                "--enable-prompt-tokens-details. cache_read_tokens=0 is unknown, not a miss."
            )
            self._vllm_cache_warning_emitted = True
        cached = _detail_value(details, "cached_tokens")
        created = _detail_value(details, "created_cache_tokens")
        prompt = int(getattr(chunk_usage, "prompt_tokens", 0) or 0)
        return Usage(
            input_tokens=max(0, prompt - cached - created),
            output_tokens=int(getattr(chunk_usage, "completion_tokens", 0) or 0),
            cache_read_tokens=cached,
            cache_creation_tokens=created,
        )


def _build_vllm_payload(
    req: ProviderRequest, options: VLLMProviderOptions | None = None
) -> dict[str, Any]:
    opts = options or VLLMProviderOptions()
    extra_body = _thinking_extra_body(req, opts)
    return _build_openai_compatible_payload(
        req,
        json_mode=opts.json_mode,
        include_stream_options=True,
        parallel_tool_calls=opts.parallel_tool_calls,
        extra_body=extra_body,
    )


def _thinking_extra_body(req: ProviderRequest, opts: VLLMProviderOptions) -> dict[str, Any] | None:
    extra_body = dict(opts.extra_body or {})
    if opts.chat_template_kwargs is not None:
        configured = extra_body.get("chat_template_kwargs")
        if isinstance(configured, dict):
            extra_body["chat_template_kwargs"] = {
                **opts.chat_template_kwargs,
                **configured,
            }
        else:
            extra_body["chat_template_kwargs"] = dict(opts.chat_template_kwargs)
    if req.thinking is not None and req.thinking.get("type") == "disabled":
        if opts.disable_thinking_kwargs is not None:
            configured = extra_body.get("chat_template_kwargs")
            merged = dict(configured) if isinstance(configured, dict) else {}
            merged.update(opts.disable_thinking_kwargs)
            extra_body["chat_template_kwargs"] = merged
    return extra_body or None


def _detail_value(details: Any, name: str) -> int:
    value = details.get(name) if isinstance(details, dict) else getattr(details, name, 0)
    return max(0, int(value or 0))
