from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal

from linch.types import ModelId, ProviderRequest

EffortLevel = Literal["low", "medium", "high", "xhigh", "max"]


@dataclass(slots=True)
class ProviderCapabilities:
    """Feature flags and limits declared by a provider implementation.

    Used by :func:`~linch.loop.apply_provider_capabilities` to downgrade
    a :class:`~linch.types.ProviderRequest` before it is sent, so that
    providers never receive fields they cannot handle.

    Attributes:
        context_window: Maximum context size in tokens for the requested
            model.
        parallel_tool_calls: Whether the provider supports executing
            multiple tool calls in a single response.  Informational — no
            request field is gated on this flag yet.
        structured_output: Whether the provider natively supports constrained
            JSON output via ``output_schema``.  When ``False``,
            ``req.output_schema`` is cleared and the loop falls back to
            text-parsing the response.
        structured_output_terminal_tool: Whether the provider implements
            ``output_schema`` by synthesising a terminal tool call.  This is
            separate from native JSON-schema and JSON-object response formats:
            only terminal-tool providers should cause the loop to treat a tool
            named after the schema as the final answer.
        tool_choice: Whether the provider honours ``tool_choice`` hints.
            When ``False``, ``req.tool_choice`` is cleared.
        prompt_cache: Whether the provider is cache-aware and should receive
            the request-level cache intent. Providers may implement this with
            explicit wire controls (Anthropic cache breakpoints, llama.cpp
            ``cache_prompt``) or with provider-native automatic prefix/session
            caching plus usage reporting (OpenAI/Gemini). When ``False``,
            those fields are cleared before the provider sees the request.
    """

    context_window: int = 128_000
    parallel_tool_calls: bool = True
    structured_output: bool = True
    structured_output_terminal_tool: bool = False
    tool_choice: bool = True
    prompt_cache: bool = False


def full_capabilities(
    context_window: int,
    *,
    parallel_tool_calls: bool = True,
    structured_output_terminal_tool: bool = False,
) -> ProviderCapabilities:
    """``ProviderCapabilities`` for providers with schema-native, prompt-cache-aware
    APIs (Anthropic, OpenAI Chat/Responses, Gemini, and OpenAI-compatible local
    servers). These providers all declare the same feature set and only differ in
    ``context_window``, optional parallel-tool support, and whether their
    structured response is represented as a terminal tool call — this factors out
    the otherwise-duplicated ``ProviderCapabilities(...)`` literal.

    Not a ``BaseProvider.capabilities()`` default: the base class stays
    conservative (``prompt_cache=False``) for providers that don't opt in.
    """
    return ProviderCapabilities(
        context_window=context_window,
        parallel_tool_calls=parallel_tool_calls,
        structured_output=True,
        structured_output_terminal_tool=structured_output_terminal_tool,
        tool_choice=True,
        prompt_cache=True,
    )


@dataclass(slots=True)
class ThinkingDisabled:
    type: Literal["disabled"] = "disabled"


@dataclass(slots=True)
class ThinkingEnabled:
    budget_tokens: int
    display: Literal["summarized", "omitted"] | None = None
    type: Literal["enabled"] = "enabled"


@dataclass(slots=True)
class ThinkingAdaptive:
    display: Literal["summarized", "omitted"] | None = None
    type: Literal["adaptive"] = "adaptive"


ThinkingConfig = ThinkingDisabled | ThinkingEnabled | ThinkingAdaptive


class BaseProvider(ABC):
    id: str

    @abstractmethod
    def context_window(self, model: ModelId) -> int:
        raise NotImplementedError

    @abstractmethod
    def stream(self, req: ProviderRequest) -> AsyncIterator[dict[str, object]]:
        raise NotImplementedError

    def capabilities(self, model: ModelId) -> ProviderCapabilities:
        """Conservative default capability set; subclasses override to declare real support."""
        return ProviderCapabilities(context_window=self.context_window(model))
