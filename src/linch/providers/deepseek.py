"""Native DeepSeek provider over its OpenAI-compatible Chat Completions API.

DeepSeek also exposes an Anthropic-shaped compatibility endpoint, but that
endpoint deliberately does not implement all current Claude features. This
adapter targets the native endpoint so thinking controls, tool-loop
``reasoning_content``, and JSON-object output use DeepSeek's documented wire
semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from linch.errors import ProviderError
from linch.providers.base import EffortLevel, ProviderCapabilities
from linch.providers.openai_chat import (
    OpenAIChatCompletionsProvider,
    OpenAIChatProviderOptions,
    _build_openai_compatible_payload,
)
from linch.types import ProviderRequest

_DEFAULT_BASE_URL = "https://api.deepseek.com"
_ThinkingMode = Literal["enabled", "disabled"]


@dataclass(slots=True)
class DeepSeekProviderOptions:
    """Options for DeepSeek's native OpenAI-compatible endpoint.

    ``thinking`` is explicit because DeepSeek enables it by default. When an
    output schema is configured, this provider always uses DeepSeek's
    ``json_object`` mode; Linch then validates the returned JSON against the
    requested schema.

    Args:
        api_key: Optional DeepSeek credential.
        base_url: DeepSeek or compatible endpoint URL.
        default_headers: Headers added to client requests.
        timeout: Optional client request timeout.
        context_window: Optional context override for the model.
        parallel_tool_calls: Optional support declaration for parallel calls.
        thinking: Whether DeepSeek reasoning is enabled by default.
        effort: Default shared reasoning effort mapped to DeepSeek's levels.
        extra_body: Additional request fields merged into the provider payload.
        include_stream_options: Whether to request usage in the final stream chunk.

    Attributes:
        thinking: Whether reasoning is enabled when a request does not override it.
        effort: Shared Linch effort level mapped to DeepSeek's ``high`` or ``max``.
        extra_body: Provider-specific fields merged into every request.
    """

    api_key: str | None = None
    base_url: str | None = _DEFAULT_BASE_URL
    default_headers: dict[str, str] | None = None
    timeout: float | None = None
    context_window: int | None = None
    parallel_tool_calls: bool | None = None
    thinking: _ThinkingMode = "enabled"
    effort: EffortLevel | None = "high"
    extra_body: dict[str, Any] | None = None
    include_stream_options: bool = True


class DeepSeekProvider(OpenAIChatCompletionsProvider):
    """DeepSeek-native Chat Completions provider.

    The parent handles streaming and, importantly, preserves
    ``reasoning_content`` as :class:`~linch.types.ThinkingBlock` on assistant
    tool-call turns. This subclass only owns DeepSeek's request extensions.

    Args:
        options: Optional DeepSeek credentials and request behavior.
    """

    id = "deepseek"

    def __init__(self, options: DeepSeekProviderOptions | None = None) -> None:
        """Create a DeepSeek provider with optional endpoint settings.

        Args:
            options: DeepSeek credentials and request behavior.
        """
        opts = options or DeepSeekProviderOptions()
        self._deepseek_options = opts
        super().__init__(
            OpenAIChatProviderOptions(
                api_key=opts.api_key,
                base_url=opts.base_url,
                default_headers=opts.default_headers,
                timeout=opts.timeout,
                context_window=opts.context_window,
                parallel_tool_calls=opts.parallel_tool_calls,
                include_stream_options=opts.include_stream_options,
            )
        )

    def _build_payload(self, req: ProviderRequest) -> dict[str, Any]:
        return _build_deepseek_payload(req, self._deepseek_options)

    def capabilities(self, model: str) -> ProviderCapabilities:
        """Return DeepSeek capabilities, including its native thinking switch.

        Args:
            model: DeepSeek model identifier.

        Returns:
            Capability flags for the selected model.
        """
        caps = super().capabilities(model)
        caps.can_disable_thinking = True
        return caps


def _build_deepseek_payload(
    req: ProviderRequest,
    options: DeepSeekProviderOptions | None = None,
) -> dict[str, Any]:
    """Build DeepSeek's documented native Chat Completions request body."""

    opts = options or DeepSeekProviderOptions()
    _validate_tool_choice(req, opts)
    extra_body = dict(opts.extra_body or {})
    extra_body["thinking"] = {"type": _effective_thinking(req, opts)}
    payload = _build_openai_compatible_payload(
        req,
        # DeepSeek supports JSON-object mode, not OpenAI's json_schema mode.
        json_mode=True,
        include_stream_options=opts.include_stream_options,
        parallel_tool_calls=opts.parallel_tool_calls,
        extra_body=extra_body,
    )
    effort = _effective_effort(req, opts)
    if effort is not None:
        payload["reasoning_effort"] = effort
    return payload


def _effective_thinking(req: ProviderRequest, opts: DeepSeekProviderOptions) -> _ThinkingMode:
    """Map Linch's shared thinking field onto DeepSeek's on/off switch."""

    if req.thinking is None:
        return opts.thinking
    return "disabled" if req.thinking.get("type") == "disabled" else "enabled"


def _effective_effort(
    req: ProviderRequest, opts: DeepSeekProviderOptions
) -> Literal["high", "max"] | None:
    """Normalize SDK effort levels to the two DeepSeek-native values."""

    configured = req.effort if req.effort is not None else opts.effort
    if configured is None or _effective_thinking(req, opts) == "disabled":
        return None
    return "max" if configured in {"xhigh", "max"} else "high"


def _validate_tool_choice(req: ProviderRequest, opts: DeepSeekProviderOptions) -> None:
    """Reject a wire combination that DeepSeek documents as unsupported.

    DeepSeek accepts normal automatic tool routing while thinking is enabled,
    which is exactly what an agentic retrieval loop needs. It rejects forced
    named/required tool selection in that mode with a provider-side 400,
    however. Raising locally preserves the caller's semantics rather than
    silently weakening a forced-tool request to ``auto``.
    """

    if _effective_thinking(req, opts) == "disabled":
        return
    if req.tool_choice is None or req.tool_choice == "auto" or req.tool_choice == "none":
        return
    raise ProviderError(
        "DeepSeek thinking mode does not support forced tool_choice. "
        "Use tool_choice='auto' for an agent loop, or disable thinking before "
        "using tool_choice='required' or a named tool."
    )


__all__ = ["DeepSeekProvider", "DeepSeekProviderOptions"]
