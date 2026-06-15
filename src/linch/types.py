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
    "interrupted",
]
PermissionMode: TypeAlias = Literal["default", "acceptEdits", "skip-dangerous"]
ModelId: TypeAlias = str


@dataclass(slots=True)
class TextBlock:
    text: str
    type: Literal["text"] = "text"


@dataclass(slots=True)
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]
    type: Literal["tool_use"] = "tool_use"


@dataclass(slots=True)
class ToolResultBlock:
    tool_use_id: str
    content: str | list[TextBlock | ImageBlock]
    is_error: bool = False
    type: Literal["tool_result"] = "tool_result"


@dataclass(slots=True)
class ThinkingBlock:
    thinking: str
    signature: str | None = None
    type: Literal["thinking"] = "thinking"


@dataclass(slots=True)
class RedactedThinkingBlock:
    data: str
    type: Literal["redacted_thinking"] = "redacted_thinking"


@dataclass(slots=True)
class ImageBlock:
    source: dict[str, str]
    type: Literal["image"] = "image"


ContentBlock: TypeAlias = (
    TextBlock | ToolUseBlock | ToolResultBlock | ThinkingBlock | RedactedThinkingBlock | ImageBlock
)


def is_text_block(block: ContentBlock) -> TypeGuard[TextBlock]:
    return isinstance(block, TextBlock)


def is_tool_use_block(block: ContentBlock) -> TypeGuard[ToolUseBlock]:
    return isinstance(block, ToolUseBlock)


def is_tool_result_block(block: ContentBlock) -> TypeGuard[ToolResultBlock]:
    return isinstance(block, ToolResultBlock)


def is_thinking_block(block: ContentBlock) -> TypeGuard[ThinkingBlock]:
    return isinstance(block, ThinkingBlock)


def is_redacted_thinking_block(block: ContentBlock) -> TypeGuard[RedactedThinkingBlock]:
    return isinstance(block, RedactedThinkingBlock)


def is_image_block(block: ContentBlock) -> TypeGuard[ImageBlock]:
    return isinstance(block, ImageBlock)


@dataclass(slots=True)
class Message:
    role: Literal["user", "assistant"]
    content: list[ContentBlock]
    provider_metadata: dict[str, Any] | None = None


@dataclass(slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    def add(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_creation_tokens=self.cache_creation_tokens + other.cache_creation_tokens,
        )


@dataclass(slots=True)
class InvokedSkillRecord:
    name: str
    substituted_body: str
    invoked_at: float = 0.0


@dataclass(slots=True)
class SkillOverlay:
    allowed_tools: list[str] | None = None
    model_override: str | None = None


def add_usage(a: Usage, b: Usage) -> Usage:
    return Usage(
        input_tokens=a.input_tokens + b.input_tokens,
        output_tokens=a.output_tokens + b.output_tokens,
        cache_read_tokens=a.cache_read_tokens + b.cache_read_tokens,
        cache_creation_tokens=a.cache_creation_tokens + b.cache_creation_tokens,
    )


@dataclass(slots=True)
class SystemBlock:
    text: str
    cacheable: bool = False
    type: Literal["text"] = "text"


@dataclass(slots=True)
class OutputSchema:
    """JSON Schema specification for constrained structured model output.

    When set on :class:`ProviderRequest`, :class:`~linch.session.RunOptions`,
    or :class:`~linch.agent.Agent`, the provider will constrain its
    response to this JSON Schema.  The parsed result surfaces as
    :attr:`~linch.events.ResultEvent.structured_output`.

    Attributes:
        name: Schema identifier sent to the provider (required by most APIs).
        schema: A valid JSON Schema ``dict`` describing the output shape.
        strict: Enable strict schema validation at the provider level
            (supported by OpenAI).  Defaults to ``True``.
        description: Optional human-readable description of the schema.
    """

    name: str
    schema: dict[str, Any]
    strict: bool = True
    description: str | None = None


# Tool-choice type: string shorthand or explicit {"type": "tool", "name": ...}
ToolChoice: TypeAlias = Literal["auto", "none", "required"] | dict[str, str]


@dataclass(slots=True)
class ProviderRequest:
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


@dataclass(slots=True)
class AssistantAssembly:
    message: Message
    stop_reason: StopReason
    usage: Usage


def block_to_dict(block: ContentBlock) -> dict[str, Any]:
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
    typ = raw.get("type")
    if typ == "text":
        return TextBlock(text=str(raw.get("text", "")))
    if typ == "tool_use":
        return ToolUseBlock(
            id=str(raw["id"]), name=str(raw["name"]), input=dict(raw.get("input", {}))
        )
    if typ == "tool_result":
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
    out = {"role": message.role, "content": [block_to_dict(block) for block in message.content]}
    if message.provider_metadata is not None:
        out["provider_metadata"] = message.provider_metadata
    return out


def message_from_dict(raw: dict[str, Any]) -> Message:
    return Message(
        role=raw["role"],
        content=[block_from_dict(block) for block in raw.get("content", [])],
        provider_metadata=raw.get("provider_metadata"),
    )
