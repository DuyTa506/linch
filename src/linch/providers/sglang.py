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
class SGLangProviderOptions:
    """Configure the SGLang OpenAI-compatible endpoint.

    Args:
        api_key: Optional endpoint credential.
        base_url: SGLang server endpoint.
        default_headers: Headers added to requests.
        context_window: Optional server context capacity in tokens.
        auto_context_window: Whether ``prepare`` probes ``/v1/models``.
        context_window_timeout: Timeout for the optional models probe in seconds.
        json_mode: Whether to request JSON-object output.
        parallel_tool_calls: Optional parallel-call support declaration.
        chat_template_kwargs: Model-specific chat-template settings.
        disable_thinking_kwargs: Model-specific settings used for disabled thinking.
        include_stream_options: Whether to request usage in the final chunk.
        sampling_params: SGLang sampling parameters merged into the request.
        enable_cache_report: Compatibility option; this is a server launch flag
            and has no request-wire effect.
        extra_body: Additional request fields merged into the payload.

    Attributes:
        json_mode: Requests JSON-object transport; Linch performs any schema
            validation after the normalized response arrives.
        sampling_params: Native SGLang sampling fields placed in ``extra_body``.
        enable_cache_report: Compatibility option for the server launch flag.
        extra_body: Provider-specific fields merged after the native SGLang fields.
    """

    api_key: str | None = None
    base_url: str | None = None
    default_headers: dict[str, str] | None = None
    context_window: int | None = None
    json_mode: bool = False
    parallel_tool_calls: bool | None = None
    # On by default so SGLang streams token usage (incl. cached_tokens with
    # enable_cache_report); set False for servers that reject stream_options.
    include_stream_options: bool = True
    sampling_params: dict[str, Any] | None = None
    enable_cache_report: bool | None = None
    extra_body: dict[str, Any] | None = None
    auto_context_window: bool = True
    context_window_timeout: float = 2.0
    chat_template_kwargs: dict[str, Any] | None = None
    disable_thinking_kwargs: dict[str, Any] | None = None


class SGLangProvider(_ChatCompletionsProvider):
    """SGLang provider using its OpenAI-compatible chat completions endpoint.

    Args:
        options: Optional SGLang endpoint and request settings.
    """

    id = "sglang"

    def __init__(self, options: SGLangProviderOptions | None = None) -> None:
        """Create an SGLang provider with optional server settings.

        Args:
            options: SGLang endpoint and request options.
        """
        opts = options or SGLangProviderOptions()
        self._sglang_options = opts
        self._context_window_cache: dict[str, int] | None = None
        super().__init__(
            OpenAIChatProviderOptions(
                api_key=opts.api_key,
                base_url=opts.base_url,
                default_headers=opts.default_headers,
                context_window=opts.context_window,
                parallel_tool_calls=opts.parallel_tool_calls,
                extra_body=_sglang_extra_body(opts),
                include_stream_options=opts.include_stream_options,
                json_mode=opts.json_mode,
            )
        )

    def _build_payload(self, req: ProviderRequest) -> dict[str, Any]:
        return _build_sglang_payload(req, self._sglang_options)

    def context_window(self, model: ModelId) -> int:
        """Return configured or probed SGLang context capacity.

        Args:
            model: Served model identifier.

        Returns:
            The configured or discovered positive token capacity.
        """
        configured = self._sglang_options.context_window
        if isinstance(configured, int) and not isinstance(configured, bool) and configured > 0:
            return configured
        return probed_context_window(self._context_window_cache, model)

    async def prepare(self) -> None:
        """Probe SGLang's model catalog once when enabled."""
        opts = self._sglang_options
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
        """Return capabilities declared for the SGLang adapter.

        Args:
            model: Served model identifier.

        Returns:
            SGLang capability flags and effective context capacity.
        """
        return ProviderCapabilities(
            context_window=self.context_window(model),
            parallel_tool_calls=self._sglang_options.parallel_tool_calls is not False,
            structured_output=True,
            tool_choice=True,
            prompt_cache=True,
            can_disable_thinking=self._sglang_options.disable_thinking_kwargs is not None,
        )

    def _reasoning_text(self, delta: Any) -> str | None:
        value = getattr(delta, "reasoning_content", None)
        return str(value) if value else None

    def _usage_from_chunk(self, chunk_usage: Any) -> Usage:
        details = getattr(chunk_usage, "prompt_tokens_details", None)
        if details is None and not getattr(self, "_sglang_cache_warning_emitted", False):
            _LOGGER.warning(
                "SGLang prompt cache token details are unavailable; start the server with "
                "--enable-cache-report. cache_read_tokens=0 is unknown, not a miss."
            )
            self._sglang_cache_warning_emitted = True
        prompt = int(getattr(chunk_usage, "prompt_tokens", 0) or 0)
        cached = _detail_value(details, "cached_tokens")
        if details is None:
            cached = max(
                cached,
                int(getattr(chunk_usage, "cached_tokens", 0) or 0),
            )
        return Usage(
            input_tokens=max(0, prompt - cached),
            output_tokens=int(getattr(chunk_usage, "completion_tokens", 0) or 0),
            cache_read_tokens=cached,
        )


def _sglang_extra_body(opts: SGLangProviderOptions) -> dict[str, Any] | None:
    extra_body: dict[str, Any] = {}
    if opts.sampling_params is not None:
        extra_body.update(opts.sampling_params)
    if opts.chat_template_kwargs is not None:
        extra_body["chat_template_kwargs"] = dict(opts.chat_template_kwargs)
    if opts.extra_body:
        explicit = dict(opts.extra_body)
        explicit.pop("enable_cache_report", None)
        explicit_chat = explicit.pop("chat_template_kwargs", None)
        if isinstance(explicit_chat, dict):
            configured_chat = extra_body.get("chat_template_kwargs")
            extra_body["chat_template_kwargs"] = {
                **(configured_chat if isinstance(configured_chat, dict) else {}),
                **explicit_chat,
            }
        elif explicit_chat is not None:
            extra_body["chat_template_kwargs"] = explicit_chat
        extra_body.update(explicit)
    return extra_body or None


def _build_sglang_payload(
    req: ProviderRequest, options: SGLangProviderOptions | None = None
) -> dict[str, Any]:
    opts = options or SGLangProviderOptions()
    extra_body = _sglang_extra_body(opts) or {}
    if req.thinking is not None and req.thinking.get("type") == "disabled":
        if opts.disable_thinking_kwargs is not None:
            configured = extra_body.get("chat_template_kwargs")
            merged = dict(configured) if isinstance(configured, dict) else {}
            merged.update(opts.disable_thinking_kwargs)
            extra_body["chat_template_kwargs"] = merged
    return _build_openai_compatible_payload(
        req,
        json_mode=opts.json_mode,
        include_stream_options=opts.include_stream_options,
        parallel_tool_calls=opts.parallel_tool_calls,
        extra_body=extra_body or None,
    )


def _detail_value(details: Any, name: str) -> int:
    value = details.get(name) if isinstance(details, dict) else getattr(details, name, 0)
    return max(0, int(value or 0))
