from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from linch.providers.openai_chat import (
    OpenAIChatCompletionsProvider,
    OpenAIChatProviderOptions,
    _build_openai_compatible_payload,
)
from linch.types import ProviderRequest


@dataclass(slots=True)
class SGLangProviderOptions:
    api_key: str | None = None
    base_url: str | None = None
    default_headers: dict[str, str] | None = None
    context_window: int = 128_000
    json_mode: bool = False
    parallel_tool_calls: bool | None = None
    # On by default so SGLang streams token usage (incl. cached_tokens with
    # enable_cache_report); set False for servers that reject stream_options.
    include_stream_options: bool = True
    sampling_params: dict[str, Any] | None = None
    enable_cache_report: bool | None = None
    extra_body: dict[str, Any] | None = None


class SGLangProvider(OpenAIChatCompletionsProvider):
    """SGLang provider using its OpenAI-compatible chat completions endpoint."""

    id = "sglang"

    def __init__(self, options: SGLangProviderOptions | None = None) -> None:
        opts = options or SGLangProviderOptions()
        self._sglang_options = opts
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


def _build_sglang_payload(
    req: ProviderRequest, options: SGLangProviderOptions | None = None
) -> dict[str, Any]:
    opts = options or SGLangProviderOptions()
    return _build_openai_compatible_payload(
        req,
        json_mode=opts.json_mode,
        include_stream_options=opts.include_stream_options,
        parallel_tool_calls=opts.parallel_tool_calls,
        extra_body=_sglang_extra_body(opts),
    )


def _sglang_extra_body(opts: SGLangProviderOptions) -> dict[str, Any] | None:
    extra_body: dict[str, Any] = {}
    if opts.sampling_params is not None:
        extra_body["sampling_params"] = opts.sampling_params
    if opts.enable_cache_report is not None:
        extra_body["enable_cache_report"] = opts.enable_cache_report
    if opts.extra_body:
        extra_body.update(opts.extra_body)
    return extra_body or None
