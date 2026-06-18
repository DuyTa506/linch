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
class VLLMProviderOptions:
    api_key: str | None = None
    base_url: str | None = None
    default_headers: dict[str, str] | None = None
    context_window: int = 128_000
    json_mode: bool = False
    parallel_tool_calls: bool | None = None
    extra_body: dict[str, Any] | None = None


class VLLMProvider(OpenAIChatCompletionsProvider):
    """vLLM provider using its OpenAI-compatible chat completions endpoint."""

    id = "vllm"

    def __init__(self, options: VLLMProviderOptions | None = None) -> None:
        opts = options or VLLMProviderOptions()
        self._vllm_options = opts
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


def _build_vllm_payload(
    req: ProviderRequest, options: VLLMProviderOptions | None = None
) -> dict[str, Any]:
    opts = options or VLLMProviderOptions()
    return _build_openai_compatible_payload(
        req,
        json_mode=opts.json_mode,
        include_stream_options=True,
        parallel_tool_calls=opts.parallel_tool_calls,
        extra_body=opts.extra_body,
    )
