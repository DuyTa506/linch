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
        structured_output: Whether the provider supports the ``output_schema``
            intent. The adapter may use native schema support, JSON-object mode,
            or loop-side validation. When ``False``, ``req.output_schema`` is cleared.
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
        can_disable_thinking: Whether the provider supports an explicit
            request-level switch that disables its reasoning/thinking mode for
            every model it serves.

    Args:
        context_window: Maximum context size supported for the selected model.
        parallel_tool_calls: Whether multiple tool calls may be returned together.
        structured_output: Whether schema-constrained output is supported.
        structured_output_terminal_tool: Whether structured output uses a terminal tool call.
        tool_choice: Whether the provider honors tool-choice hints.
        prompt_cache: Whether request-level prompt-cache controls are supported.
        can_disable_thinking: Whether request-level thinking can be disabled safely.
    """

    context_window: int = 128_000
    parallel_tool_calls: bool = True
    structured_output: bool = True
    structured_output_terminal_tool: bool = False
    tool_choice: bool = True
    prompt_cache: bool = False
    can_disable_thinking: bool = False
    catalog_changes: Literal["append_only", "positional"] = "append_only"


def full_capabilities(
    context_window: int,
    *,
    parallel_tool_calls: bool = True,
    structured_output_terminal_tool: bool = False,
    can_disable_thinking: bool = False,
) -> ProviderCapabilities:
    """Build capabilities for providers with schema/output and prompt-cache support
    APIs (Anthropic, OpenAI Chat/Responses, Gemini, and OpenAI-compatible local
    servers). These providers all declare the same feature set and only differ in
    ``context_window``, optional parallel-tool support, and whether their
    structured response is represented as a terminal tool call — this factors out
    the otherwise-duplicated ``ProviderCapabilities(...)`` literal.

    Not a ``BaseProvider.capabilities()`` default: the base class stays
    conservative (``prompt_cache=False``) for providers that don't opt in.

    Args:
        context_window: Maximum context size supported by the provider.
        parallel_tool_calls: Whether parallel tool calls are supported.
        structured_output_terminal_tool: Whether structured output is represented as
            a terminal tool.
        can_disable_thinking: Whether request-level thinking can be disabled safely.

    Returns:
        Capability flags for a provider that opts into its adapter-specific
        structured-output and prompt-cache behavior.
    """
    return ProviderCapabilities(
        context_window=context_window,
        parallel_tool_calls=parallel_tool_calls,
        structured_output=True,
        structured_output_terminal_tool=structured_output_terminal_tool,
        tool_choice=True,
        prompt_cache=True,
        can_disable_thinking=can_disable_thinking,
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
    """Define the normalized provider interface consumed by the agent loop.

    Implementations keep SDK-specific objects inside the adapter and expose
    normalized event dictionaries through :meth:`stream`.

    Attributes:
        id: Stable provider identifier used in diagnostics and model metadata.
    """

    id: str

    @abstractmethod
    def context_window(self, model: ModelId) -> int:
        """Return the positive token capacity for a model.

        Args:
            model: Provider model identifier to look up.

        Returns:
            The model's maximum context window in tokens.
        """
        raise NotImplementedError

    @abstractmethod
    def stream(self, req: ProviderRequest) -> AsyncIterator[dict[str, object]]:
        """Return an async iterator of normalized provider events.

        Args:
            req: Normalized request to send to the provider.

        Returns:
            An async iterator yielding the event dictionaries described by the
            provider contract; raw SDK objects must remain inside the adapter.
        """
        raise NotImplementedError

    def capabilities(self, model: ModelId) -> ProviderCapabilities:
        """Return a conservative capability set for a model.

        Args:
            model: Provider model identifier to inspect.

        Returns:
            Capabilities containing the model context window and the dataclass
            defaults for parallel tools, structured output, and tool choice;
            prompt caching remains disabled unless a subclass opts in.
        """
        return ProviderCapabilities(context_window=self.context_window(model))
