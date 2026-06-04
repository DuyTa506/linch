from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from linch.providers.base import ProviderCapabilities
from linch.providers.openai_chat import (
    OpenAIChatCompletionsProvider,
    OpenAIChatProviderOptions,
    _build_chat_payload,
)
from linch.types import ModelId, ProviderRequest


@dataclass(slots=True)
class LlamaCppProviderOptions:
    api_key: str | None = None
    base_url: str | None = None
    default_headers: dict[str, str] | None = None
    context_window: int = 128_000
    json_mode: bool = False
    parallel_tool_calls: bool | None = None
    chat_template_kwargs: dict[str, Any] | None = None
    reasoning_format: str | None = None
    reasoning_control: bool | None = None
    generation_prompt: str | None = None
    parse_tool_calls: bool | None = None
    extra_body: dict[str, Any] | None = None


class LlamaCppProvider(OpenAIChatCompletionsProvider):
    """llama.cpp server provider using its OpenAI-compatible chat endpoint."""

    id = "llamacpp"

    def __init__(self, options: LlamaCppProviderOptions | None = None) -> None:
        opts = options or LlamaCppProviderOptions()
        self._llamacpp_options = opts
        super().__init__(
            OpenAIChatProviderOptions(
                api_key=opts.api_key,
                base_url=opts.base_url,
                default_headers=opts.default_headers,
                json_mode=False,
            )
        )

    def context_window(self, model: ModelId) -> int:
        return self._llamacpp_options.context_window

    def capabilities(self, model: ModelId) -> ProviderCapabilities:
        return ProviderCapabilities(
            context_window=self.context_window(model),
            parallel_tool_calls=self._llamacpp_options.parallel_tool_calls is not False,
            structured_output=True,
            tool_choice=True,
            prompt_cache=False,
        )

    def _build_payload(self, req: ProviderRequest) -> dict[str, Any]:
        return _build_llamacpp_payload(req, self._llamacpp_options)


def _build_llamacpp_payload(
    req: ProviderRequest, options: LlamaCppProviderOptions | None = None
) -> dict[str, Any]:
    opts = options or LlamaCppProviderOptions()
    payload = _build_chat_payload(req, json_mode=False)

    # llama.cpp does not document OpenAI's stream_options field and older
    # server builds reject unknown request keys.
    payload.pop("stream_options", None)

    if req.output_schema is not None:
        if opts.json_mode:
            payload["response_format"] = {"type": "json_object"}
        else:
            payload["response_format"] = {
                "type": "json_schema",
                "schema": req.output_schema.schema,
            }

    extra_body = dict(opts.extra_body or {})
    if opts.chat_template_kwargs is not None:
        extra_body["chat_template_kwargs"] = opts.chat_template_kwargs
    if opts.reasoning_format is not None:
        extra_body["reasoning_format"] = opts.reasoning_format
    if opts.reasoning_control is not None:
        extra_body["reasoning_control"] = opts.reasoning_control
    if opts.generation_prompt is not None:
        extra_body["generation_prompt"] = opts.generation_prompt
    if opts.parse_tool_calls is not None:
        extra_body["parse_tool_calls"] = opts.parse_tool_calls
    if extra_body:
        payload["extra_body"] = extra_body

    if opts.parallel_tool_calls is not None:
        payload["parallel_tool_calls"] = opts.parallel_tool_calls

    return payload
