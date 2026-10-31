from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, TypeAlias, TypeGuard

StopReason: TypeAlias = Literal[
    "end_turn",
    "tool_use",
    "max_tokens",
    "stop_sequence",
    "refusal",
    "error",
    "lease_lost",
    "interrupted",
]
PermissionMode: TypeAlias = Literal["default", "acceptEdits", "skip-dangerous"]
ModelId: TypeAlias = str


@dataclass(slots=True)
class TextBlock:
    """Represent plain text in a model message.

    Args:
        text: Text content to expose to the provider.
        type: Wire discriminator for this content block.
    """

    text: str
    type: Literal["text"] = "text"


@dataclass(slots=True)
class ToolUseBlock:
    """Represent a model request to invoke a tool.

    Attributes:
        input: JSON-compatible arguments the provider supplied for the call.

    Args:
        id: Provider-assigned identifier for this tool call.
        name: Name of the tool to invoke.
        input: JSON-compatible arguments supplied to the tool.
        type: Wire discriminator for this content block.
    """

    id: str
    name: str
    input: dict[str, Any]
    type: Literal["tool_use"] = "tool_use"


@dataclass(slots=True)
class ToolResultBlock:
    """Represent the result returned for a tool call.

    Attributes:
        tool_use_id: Correlation identifier matching the originating
            :class:`ToolUseBlock`.

    Args:
        tool_use_id: Identifier of the tool call that produced the result.
        content: Text or multimodal content returned by the tool.
        is_error: Whether the tool reported an error.
        type: Wire discriminator for this content block.
    """

    tool_use_id: str
    content: str | list[TextBlock | ImageBlock]
    is_error: bool = False
    type: Literal["tool_result"] = "tool_result"


@dataclass(slots=True)
class ThinkingBlock:
    """Represent provider reasoning content.

    Attributes:
        signature: Optional opaque provider signature associated with the
            reasoning text.

    Args:
        thinking: Reasoning text emitted by the provider.
        signature: Optional provider signature associated with the reasoning.
        type: Wire discriminator for this content block.
    """

    thinking: str
    signature: str | None = None
    type: Literal["thinking"] = "thinking"


@dataclass(slots=True)
class RedactedThinkingBlock:
    """Represent provider reasoning content that was redacted.

    Attributes:
        data: Opaque provider payload retained for the redacted reasoning block.

    Args:
        data: Opaque redacted payload supplied by the provider.
        type: Wire discriminator for this content block.
    """

    data: str
    type: Literal["redacted_thinking"] = "redacted_thinking"


@dataclass(slots=True)
class ImageBlock:
    """Represent an image supplied as message content.

    Attributes:
        source: Provider-specific descriptor, commonly containing a media type
            and URL or encoded image data.

    Args:
        source: Provider-specific image source descriptor.
        type: Wire discriminator for this content block.
    """

    source: dict[str, str]
    type: Literal["image"] = "image"


ContentBlock: TypeAlias = (
    TextBlock | ToolUseBlock | ToolResultBlock | ThinkingBlock | RedactedThinkingBlock | ImageBlock
)


def is_text_block(block: ContentBlock) -> TypeGuard[TextBlock]:
    """Return whether a content block contains plain text.

    Args:
        block: Content block to inspect.

    Returns:
        Whether ``block`` is a :class:`TextBlock`.
    """

    return isinstance(block, TextBlock)


def is_tool_use_block(block: ContentBlock) -> TypeGuard[ToolUseBlock]:
    """Return whether a content block requests a tool invocation.

    Args:
        block: Content block to inspect.

    Returns:
        Whether ``block`` is a :class:`ToolUseBlock`.
    """

    return isinstance(block, ToolUseBlock)


def is_tool_result_block(block: ContentBlock) -> TypeGuard[ToolResultBlock]:
    """Return whether a content block contains a tool result.

    Args:
        block: Content block to inspect.

    Returns:
        Whether ``block`` is a :class:`ToolResultBlock`.
    """

    return isinstance(block, ToolResultBlock)


def is_thinking_block(block: ContentBlock) -> TypeGuard[ThinkingBlock]:
    """Return whether a content block contains provider reasoning.

    Args:
        block: Content block to inspect.

    Returns:
        Whether ``block`` is a :class:`ThinkingBlock`.
    """

    return isinstance(block, ThinkingBlock)


def is_redacted_thinking_block(block: ContentBlock) -> TypeGuard[RedactedThinkingBlock]:
    """Return whether a content block contains redacted reasoning.

    Args:
        block: Content block to inspect.

    Returns:
        Whether ``block`` is a :class:`RedactedThinkingBlock`.
    """

    return isinstance(block, RedactedThinkingBlock)


def is_image_block(block: ContentBlock) -> TypeGuard[ImageBlock]:
    """Return whether a content block contains an image.

    Args:
        block: Content block to inspect.

    Returns:
        Whether ``block`` is an :class:`ImageBlock`.
    """

    return isinstance(block, ImageBlock)


@dataclass(slots=True)
class Message:
    """Represent one user or assistant message.

    Attributes:
        provider_metadata: Optional provider-specific fields preserved across
            serialization without changing the normalized message shape.

    Args:
        role: Role that authored the message.
        content: Ordered content blocks in the message.
        provider_metadata: Optional provider-specific metadata to preserve.
    """

    role: Literal["user", "assistant"]
    content: list[ContentBlock]
    provider_metadata: dict[str, Any] | None = None


@dataclass(slots=True)
class Usage:
    """Track token usage reported for a provider call or run.

    Attributes:
        cache_read_tokens: Input tokens served from a provider prompt cache.
        cache_creation_tokens: Input tokens written to a provider prompt cache.

    Args:
        input_tokens: Number of input tokens consumed.
        output_tokens: Number of output tokens consumed.
        cache_read_tokens: Number of cached input tokens read.
        cache_creation_tokens: Number of tokens written to a provider cache.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    def add(self, other: Usage) -> Usage:
        """Return usage totals obtained by adding another usage record.

        Args:
            other: Usage values to add to this record.

        Returns:
            A new usage record containing the field-wise totals.
        """

        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_creation_tokens=self.cache_creation_tokens + other.cache_creation_tokens,
        )


@dataclass(slots=True)
class InvokedSkillRecord:
    """Record a skill body that was invoked during a run.

    Args:
        name: Name of the invoked skill.
        substituted_body: Skill body after argument substitution.
        invoked_at: Timestamp associated with the invocation.
    """

    name: str
    substituted_body: str
    invoked_at: float = 0.0


@dataclass(slots=True)
class SkillOverlay:
    """Describe temporary model and tool restrictions for a skill.

    Attributes:
        allowed_tools: When set, limits the tools available during the skill.
        model_override: When set, selects the skill's temporary model.

    Args:
        allowed_tools: Optional tool allowlist for the skill invocation.
        model_override: Optional model to use while the skill runs.
    """

    allowed_tools: list[str] | None = None
    model_override: str | None = None


@dataclass(slots=True)
class SystemBlock:
    """Represent one system-prompt block.

    Attributes:
        cacheable: Marks a stable prefix that may be reused by provider prompt
            caching.

    Args:
        text: Prompt text supplied to the provider.
        cacheable: Whether this block may participate in provider prefix caching.
        type: Wire discriminator for this system block.
    """

    text: str
    cacheable: bool = False
    type: Literal["text"] = "text"


@dataclass(slots=True)
class OutputSchema:
    """JSON Schema specification for constrained structured model output.

    Providers that support structured output can constrain the response to this
    JSON Schema; otherwise the loop parses and validates returned text. The
    parsed result surfaces as :attr:`~linch.events.ResultEvent.structured_output`.

    Attributes:
        name: Schema identifier sent to the provider (required by most APIs).
        schema: A valid JSON Schema ``dict`` describing the output shape.
        strict: Enable strict schema validation at the provider level
            (supported by OpenAI).  Defaults to ``True``.
        description: Optional human-readable description of the schema.

    Args:
        name: Schema identifier sent to the provider.
        schema: JSON Schema describing the output shape.
        strict: Whether provider-side strict validation is requested.
        description: Optional human-readable schema description.
    """

    name: str
    schema: dict[str, Any]
    strict: bool = True
    description: str | None = None


# Tool-choice type: string shorthand or explicit {"type": "tool", "name": ...}
ToolChoice: TypeAlias = Literal["auto", "none", "required"] | dict[str, str]


@dataclass(slots=True)
class ProviderRequest:
    """Collect the normalized inputs for one provider call.

    Attributes:
        signal: Optional host cancellation signal passed to the provider.
        stream_partials: ``None`` inherits the agent setting; ``False`` still
            assembles deltas while suppressing raw partial-event projection.

    Args:
        model: Model identifier requested for the call.
        system: System-prompt blocks in provider order.
        tools: Tool schemas offered to the model.
        messages: Conversation messages included in the request.
        signal: Optional cancellation signal supplied by the caller.
        max_output_tokens: Optional output token limit.
        temperature: Optional sampling temperature.
        stop_sequences: Optional provider stop sequences.
        max_retries: Maximum provider retry attempts.
        reasoning: Optional provider reasoning configuration.
        cache_prompt: Whether prompt caching is requested.
        cache_ttl: Requested prompt-cache lifetime.
        thinking: Optional provider thinking configuration.
        effort: Optional reasoning effort level.
        output_schema: Optional schema for structured output.
        tool_choice: Optional tool-selection policy.
        stream_partials: Whether raw partial events should be projected.
    """

    model: str
    system: list[SystemBlock]
    tools: list[dict[str, Any]]
    messages: list[Message]
    signal: Any = None
    max_output_tokens: int | None = None
    temperature: float | None = None
    stop_sequences: list[str] | None = None
    max_retries: int = 5
    reasoning: dict[str, Any] | None = None
    cache_prompt: bool | None = None
    cache_ttl: Literal["5m", "1h"] | None = None
    thinking: dict[str, Any] | None = None
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    output_schema: OutputSchema | None = None
    tool_choice: ToolChoice | None = None
    # ``None`` preserves the agent-level partial-event setting. ``False`` keeps
    # consuming and assembling provider deltas but suppresses their raw
    # ``PartialAssistantEvent`` projection for this request.
    stream_partials: bool | None = None


@dataclass(slots=True)
class AssistantAssembly:
    """Hold the assembled assistant message and provider accounting.

    Args:
        message: Assistant message assembled from provider deltas.
        stop_reason: Provider reason for ending the response.
        usage: Token usage reported for the response.
    """

    message: Message
    stop_reason: StopReason
    usage: Usage


def block_to_dict(block: ContentBlock) -> dict[str, Any]:
    """Serialize a content block to its wire representation.

    Args:
        block: Content block to encode.

    Returns:
        Wire-shaped content-block mapping.

    Raises:
        TypeError: If ``block`` is not a supported content-block instance.
    """

    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ToolUseBlock):
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if isinstance(block, ToolResultBlock):
        content = block.content
        if isinstance(content, list):
            content = [block_to_dict(item) for item in content]
        out: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": block.tool_use_id,
            "content": content,
        }
        if block.is_error:
            out["is_error"] = True
        return out
    if isinstance(block, ThinkingBlock):
        out = {"type": "thinking", "thinking": block.thinking}
        if block.signature is not None:
            out["signature"] = block.signature
        return out
    if isinstance(block, RedactedThinkingBlock):
        return {"type": "redacted_thinking", "data": block.data}
    if isinstance(block, ImageBlock):
        return {"type": "image", "source": block.source}
    raise TypeError(f"unknown block {block!r}")


def block_from_dict(raw: dict[str, Any]) -> ContentBlock:
    """Deserialize a wire mapping into a content block.

    Args:
        raw: Wire mapping containing a supported ``type`` discriminator.

    Returns:
        Parsed content block.

    Raises:
        KeyError: If a required field is missing.
        TypeError: If a field cannot be converted to the expected container.
        ValueError: If the discriminator or fields are invalid.
    """

    typ = raw.get("type")
    if typ == "text":
        return TextBlock(text=str(raw.get("text", "")))
    if typ == "tool_use":
        return ToolUseBlock(
            id=str(raw["id"]), name=str(raw["name"]), input=dict(raw.get("input", {}))
        )
    if typ == "tool_result":
        unknown_fields = raw.keys() - {"type", "tool_use_id", "content", "is_error"}
        if unknown_fields:
            raise ValueError(f"unknown tool_result fields: {sorted(unknown_fields)!r}")
        raw_content = raw.get("content", "")
        content: str | list[TextBlock | ImageBlock]
        if isinstance(raw_content, list):
            nested: list[TextBlock | ImageBlock] = []
            for item in raw_content:
                if not isinstance(item, dict):
                    continue
                block = block_from_dict(item)
                if isinstance(block, TextBlock | ImageBlock):
                    nested.append(block)
            content = nested
        else:
            content = str(raw_content)
        return ToolResultBlock(
            tool_use_id=str(raw["tool_use_id"]),
            content=content,
            is_error=bool(raw.get("is_error", False)),
        )
    if typ == "thinking":
        return ThinkingBlock(
            thinking=str(raw.get("thinking", "")),
            signature=raw.get("signature") if isinstance(raw.get("signature"), str) else None,
        )
    if typ == "redacted_thinking":
        return RedactedThinkingBlock(data=str(raw.get("data", "")))
    if typ == "image":
        return ImageBlock(source=dict(raw.get("source", {})))
    raise ValueError(f"unknown content block type: {typ!r}")


def message_to_dict(message: Message) -> dict[str, Any]:
    """Serialize a message to its provider-neutral wire representation.

    Args:
        message: Message to encode.

    Returns:
        Wire-shaped message mapping.
    """

    out = {"role": message.role, "content": [block_to_dict(block) for block in message.content]}
    if message.provider_metadata is not None:
        out["provider_metadata"] = message.provider_metadata
    return out


def message_from_dict(raw: dict[str, Any]) -> Message:
    """Deserialize a provider-neutral mapping into a message.

    Args:
        raw: Wire mapping containing a role and content list.

    Returns:
        Parsed message.

    Raises:
        KeyError: If the required role or nested block field is missing.
        TypeError: If a field cannot be converted to the expected container.
        ValueError: If a nested content block is invalid.
    """

    return Message(
        role=raw["role"],
        content=[block_from_dict(block) for block in raw.get("content", [])],
        provider_metadata=raw.get("provider_metadata"),
    )
