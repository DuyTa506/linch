"""What a provider supports and what its server lists: plain data."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

JsonObject = dict[str, Any]


@dataclass(frozen=True)
class ToolChoiceCaps:
    """Which `tool_choice` forms a provider takes.

    Attributes:
        auto: `auto` is taken.
        none: `none` is taken.
        required: `required` is taken, with thinking off.
        named: A named tool is taken, with thinking off.
        forced_refused_with: The thinking states (`unset`, `adaptive`,
            `budgeted`) in which `required` and a named tool are refused
            although thinking off takes them; empty when thinking never
            matters. `unset` is a request with no `thinking`; DeepSeek
            thinks by default, so it refuses there.
    """

    auto: bool
    none: bool
    required: bool
    named: bool
    forced_refused_with: tuple[str, ...] = ()


@dataclass(frozen=True)
class ThinkingCaps:
    """What the request's reasoning controls do.

    Attributes:
        disable: `thinking: disabled` turns reasoning off.
        budget: `thinking: enabled` with `budget_tokens` bounds it.
        effort: `effort` is sent.
        reasoning_text: The stream carries the model's reasoning text.
    """

    disable: bool
    budget: bool
    effort: bool
    reasoning_text: bool


@dataclass(frozen=True)
class ProviderCapabilities:
    """What one provider adapter supports for one model.

    Attributes:
        provider: The provider's id.
        model: The model asked about.
        declared: Whether the adapter declares the fields after
            `output_schema`; false for a provider (such as a test double)
            that declares nothing.
        context_window: The most tokens a request takes, as a run would
            use it.
        context_source: `configured`, `server` (the server said so),
            `table` (the adapter's own table) or `default` (a guess).
        output_schema: How a run with an output schema gets its structured
            answer. `native`: the schema goes in the request in the provider's
            own structured-output form. `final_tool`: the request carries no
            schema and the run offers a final tool whose input is the schema.
            `final_tool` does not mean the server cannot do structured
            output: vLLM takes a `response_format`, but with tools in the same
            request its guided decoding binds the whole answer to the schema
            and the model never calls a tool, so a run there uses the final
            tool.
        tool_choice: The `tool_choice` forms taken.
        thinking: The reasoning controls.
        vision: Image input: `supported`, `unsupported`, `model_dependent`
            (the adapter sends images and the served model decides) or
            `unknown`.
        streams_tool_arguments: A tool call's arguments stream in pieces.
        prompt_cache: `none`, `automatic` (the server caches by itself) or
            `explicit` (a request's `cache_control` marks what is cached).
        stop_sequences: `stop_sequences` are sent, not refused.
    """

    provider: str
    model: str
    declared: bool
    context_window: int
    context_source: str
    output_schema: str
    tool_choice: ToolChoiceCaps
    thinking: ThinkingCaps
    vision: str
    streams_tool_arguments: bool
    prompt_cache: str
    stop_sequences: bool

    @classmethod
    def from_wire(cls, raw: JsonObject) -> ProviderCapabilities:
        """Decode the `provider.capabilities` result."""
        choice = raw["tool_choice"]
        thinking = raw["thinking"]
        return cls(
            provider=str(raw["provider"]),
            model=str(raw["model"]),
            declared=bool(raw["declared"]),
            context_window=int(raw["context_window"]),
            context_source=str(raw["context_source"]),
            output_schema=str(raw["output_schema"]),
            tool_choice=ToolChoiceCaps(
                auto=bool(choice["auto"]),
                none=bool(choice["none"]),
                required=bool(choice["required"]),
                named=bool(choice["named"]),
                forced_refused_with=tuple(str(s) for s in choice["forced_refused_with"]),
            ),
            thinking=ThinkingCaps(
                disable=bool(thinking["disable"]),
                budget=bool(thinking["budget"]),
                effort=bool(thinking["effort"]),
                reasoning_text=bool(thinking["reasoning_text"]),
            ),
            vision=str(raw["vision"]),
            streams_tool_arguments=bool(raw["streams_tool_arguments"]),
            prompt_cache=str(raw["prompt_cache"]),
            stop_sequences=bool(raw["stop_sequences"]),
        )


@dataclass(frozen=True)
class ModelInfo:
    """One model a provider's server lists; a field the API leaves out is
    `None` or empty, never a guess.

    Attributes:
        id: The id a request names the model by.
        display_name: A human-readable name.
        context_window: The most input tokens it takes.
        max_output_tokens: The most tokens it generates in one answer.
        input_modalities: What it takes as input (`text`, `image`), when
            the listing says.
    """

    id: str
    display_name: str | None = None
    context_window: int | None = None
    max_output_tokens: int | None = None
    input_modalities: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_wire(cls, raw: JsonObject) -> ModelInfo:
        """Decode one model of `provider.models` or the `provider.model` result."""
        window = raw.get("context_window")
        output = raw.get("max_output_tokens")
        return cls(
            id=str(raw["id"]),
            display_name=raw.get("display_name"),
            context_window=None if window is None else int(window),
            max_output_tokens=None if output is None else int(output),
            input_modalities=tuple(str(m) for m in raw.get("input_modalities", [])),
        )


class ModelList(list[ModelInfo]):
    """The models a provider's server lists, in its order: a `list` of
    `ModelInfo` that also says whether the list was cut.

    Attributes:
        truncated: The provider had more pages than `linch-server` follows
            (10), so the list holds only the first of them. A model that is
            not in a truncated list may still exist.
    """

    truncated: bool

    def __init__(self, models: Iterable[ModelInfo] = (), truncated: bool = False) -> None:
        super().__init__(models)
        self.truncated = truncated
