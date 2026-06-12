from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, cast

from linch.errors import (
    AbortError,
    AuthError,
    ContextLengthError,
    ProviderError,
    RateLimitError,
)
from linch.providers.base import BaseProvider, ProviderCapabilities
from linch.types import (
    ImageBlock,
    Message,
    ModelId,
    ProviderRequest,
    StopReason,
    SystemBlock,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)

_KNOWN_CONTEXT = {
    "claude-opus-4-8": 200_000,
    "claude-sonnet-4-6": 200_000,
    "claude-haiku-4-5": 200_000,
}

# Anthropic requires max_tokens; use this when the caller doesn't set one.
_DEFAULT_MAX_TOKENS = 8096


@dataclass(slots=True)
class AnthropicProviderOptions:
    api_key: str | None = None
    base_url: str | None = None
    default_headers: dict[str, str] | None = None
    thinking: object | None = None
    effort: str | None = None


class AnthropicProvider(BaseProvider):
    id = "anthropic"

    def __init__(self, options: AnthropicProviderOptions | None = None) -> None:
        self._options = options or AnthropicProviderOptions()
        self._client: Any | None = None

    def context_window(self, model: ModelId) -> int:
        return _KNOWN_CONTEXT.get(model, 200_000)

    def capabilities(self, model: ModelId) -> ProviderCapabilities:
        return ProviderCapabilities(
            context_window=self.context_window(model),
            parallel_tool_calls=True,
            structured_output=True,  # forced-tool method (Feature A)
            tool_choice=True,
            prompt_cache=True,
        )

    async def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import anthropic
        except ModuleNotFoundError as exc:
            raise ProviderError(
                "The 'anthropic' package is required. Install with: pip install 'linch[anthropic]'"
            ) from exc
        AsyncAnthropic = cast(Any, anthropic).AsyncAnthropic
        kwargs: dict[str, Any] = {}
        if self._options.api_key is not None:
            kwargs["api_key"] = self._options.api_key
        if self._options.base_url is not None:
            kwargs["base_url"] = self._options.base_url
        if self._options.default_headers is not None:
            kwargs["default_headers"] = self._options.default_headers
        self._client = AsyncAnthropic(**kwargs)
        return self._client

    async def stream(self, req: ProviderRequest) -> AsyncIterator[dict[str, object]]:
        client = await self._get_client()
        payload = _build_payload(req, self._options)

        # Emit immediately so callers receive model info without waiting for chunks.
        yield {"type": "message_start", "model": req.model}

        input_tokens = 0
        cache_read_tokens = 0
        cache_creation_tokens = 0
        output_tokens = 0
        stop_reason: StopReason = "end_turn"
        # Maps content-block index → tool_use id for correlating deltas and stops.
        tool_idx: dict[int, str] = {}

        try:
            raw_stream = await client.messages.create(**payload)
            async for event in raw_stream:
                etype = event.type

                if etype == "message_start":
                    u = event.message.usage
                    input_tokens = int(u.input_tokens or 0)
                    cache_read_tokens = int(getattr(u, "cache_read_input_tokens", 0) or 0)
                    cache_creation_tokens = int(getattr(u, "cache_creation_input_tokens", 0) or 0)

                elif etype == "content_block_start":
                    cb = event.content_block
                    if cb.type == "tool_use":
                        tool_idx[event.index] = cb.id
                        yield {"type": "tool_use_start", "id": cb.id, "name": cb.name}
                    # text / thinking blocks: their deltas follow separately

                elif etype == "content_block_delta":
                    delta = event.delta
                    dtype = delta.type
                    if dtype == "text_delta":
                        yield {"type": "text_delta", "text": delta.text}
                    elif dtype == "input_json_delta":
                        tid = tool_idx.get(event.index, "")
                        yield {
                            "type": "tool_use_input_delta",
                            "id": tid,
                            "json_delta": delta.partial_json,
                        }
                    elif dtype == "thinking_delta":
                        yield {"type": "thinking_delta", "text": delta.thinking}
                    elif dtype == "signature_delta":
                        # Carry the signature on a zero-text thinking_delta so
                        # stream_turn in loop.py can store it in thinking_sig.
                        yield {
                            "type": "thinking_delta",
                            "text": "",
                            "signature": delta.signature,
                        }

                elif etype == "content_block_stop":
                    tid = tool_idx.pop(event.index, None)
                    if tid is not None:
                        yield {"type": "tool_use_end", "id": tid}

                elif etype == "message_delta":
                    output_tokens = int(event.usage.output_tokens or 0)
                    # Cache figures are cumulative/authoritative (set at
                    # message_start); if a delta restates them, overwrite —
                    # don't add — and only when the field is actually present
                    # so an omitted field doesn't clobber the start value to 0.
                    cr = getattr(event.usage, "cache_read_input_tokens", None)
                    if cr is not None:
                        cache_read_tokens = int(cr or 0)
                    cc = getattr(event.usage, "cache_creation_input_tokens", None)
                    if cc is not None:
                        cache_creation_tokens = int(cc or 0)
                    stop_reason = _map_stop_reason(getattr(event.delta, "stop_reason", None))

                # message_stop signals end of stream; handled after the loop.

        except asyncio.CancelledError as exc:
            raise AbortError("aborted") from exc
        except Exception as exc:
            raise _map_anthropic_error(exc) from exc

        yield {
            "type": "message_end",
            "stop_reason": stop_reason,
            "usage": Usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_creation_tokens=cache_creation_tokens,
            ),
            "provider_metadata": None,
        }


# ── Payload builder ────────────────────────────────────────────────────────


def _build_payload(req: ProviderRequest, opts: AnthropicProviderOptions) -> dict[str, Any]:
    """Translate a :class:`ProviderRequest` into an Anthropic API payload dict."""
    payload: dict[str, Any] = {
        "model": req.model,
        "max_tokens": req.max_output_tokens or _DEFAULT_MAX_TOKENS,
        "stream": True,
    }

    # ── System ────────────────────────────────────────────────────────────
    system_blocks = _translate_system(req.system, req.cache_prompt, req.cache_ttl)
    if system_blocks:
        payload["system"] = system_blocks

    # ── Messages ──────────────────────────────────────────────────────────
    payload["messages"] = _translate_messages(req.messages)

    # ── Tools ─────────────────────────────────────────────────────────────
    if req.tools:
        payload["tools"] = _translate_tools(req.tools, req.cache_prompt, req.cache_ttl)

    # ── Optional scalar fields ────────────────────────────────────────────
    if req.temperature is not None:
        payload["temperature"] = req.temperature
    if req.stop_sequences:
        payload["stop_sequences"] = req.stop_sequences

    # ── Tool choice ───────────────────────────────────────────────────────
    if req.tool_choice is not None:
        payload["tool_choice"] = _translate_tool_choice(req.tool_choice)

    # ── Output schema (Anthropic forced-tool method) ──────────────────────
    # Anthropic has no response_format; structured output is achieved by
    # synthesising a tool whose input_schema matches the caller's schema and
    # forcing the model to call it.  The loop terminal-tool path then captures
    # final_block.input as structured_output without executing a real tool.
    if req.output_schema is not None:
        schema_tool: dict[str, Any] = {
            "name": req.output_schema.name,
            "description": req.output_schema.description or "",
            "input_schema": req.output_schema.schema,
        }
        existing_tools = list(payload.get("tools", []))
        payload["tools"] = existing_tools + [schema_tool]
        # Force the schema tool only when it is the sole tool available so the
        # model cannot bypass it.  When real tools are also present use "auto"
        # so the model can call them first and invoke the schema tool when ready.
        if not existing_tools:
            payload["tool_choice"] = {"type": "tool", "name": req.output_schema.name}

    # ── Thinking ──────────────────────────────────────────────────────────
    # req.thinking wins over constructor-level options.thinking.
    thinking_cfg = req.thinking or (opts.thinking if isinstance(opts.thinking, dict) else None)
    if thinking_cfg:
        payload["thinking"] = thinking_cfg

    return payload


def _translate_system(
    blocks: list[SystemBlock],
    cache: bool | None,
    ttl: str | None,
) -> list[dict[str, Any]]:
    """Convert system blocks to the Anthropic system array, with optional caching.

    The cache breakpoint is placed at the end of the leading contiguous run of
    static (``cacheable=True``) blocks (ROADMAP 3.2). A volatile trailing block
    (``cacheable=False``, e.g. a per-turn dynamic section) therefore sits outside
    the cached prefix and never invalidates it. When every block is static this
    is the last block — byte-identical to the legacy "cache the last block"
    behavior for the all-static prompts the agent builds today.
    """
    if not blocks:
        return []
    result: list[dict[str, Any]] = [{"type": "text", "text": block.text} for block in blocks]
    if not cache:
        return result
    boundary = _cache_breakpoint_index(blocks)
    if boundary is not None:
        cc: dict[str, Any] = {"type": "ephemeral"}
        if ttl:
            cc["ttl"] = ttl
        result[boundary]["cache_control"] = cc
    return result


def _cache_breakpoint_index(blocks: list[SystemBlock]) -> int | None:
    """Index of the last block in the leading contiguous ``cacheable`` run.

    Returns ``None`` when the first block is already dynamic — there is then no
    static prefix to cache.
    """
    end = -1
    for index, block in enumerate(blocks):
        if not getattr(block, "cacheable", False):
            break
        end = index
    return end if end >= 0 else None


def _translate_tools(
    tools: list[dict[str, Any]],
    cache: bool | None,
    ttl: str | None,
) -> list[dict[str, Any]]:
    """Convert Linch tool schemas to Anthropic ToolParam dicts."""
    result: list[dict[str, Any]] = []
    for i, tool in enumerate(tools):
        item: dict[str, Any] = {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "input_schema": tool["input_schema"],
        }
        if cache and i == len(tools) - 1:
            cc: dict[str, Any] = {"type": "ephemeral"}
            if ttl:
                cc["ttl"] = ttl
            item["cache_control"] = cc
        result.append(item)
    return result


def _translate_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Convert internal Message objects to Anthropic message dicts."""
    result: list[dict[str, Any]] = []
    for msg in messages:
        if msg.role == "assistant":
            content = _translate_assistant_content(msg.content)
            if content:
                result.append({"role": "assistant", "content": content})
        else:
            # user messages may interleave text, images, and tool results
            text_parts: list[dict[str, Any]] = []
            for block in msg.content:
                if isinstance(block, ToolResultBlock):
                    # Flush accumulated text parts before emitting tool result
                    if text_parts:
                        result.append({"role": "user", "content": text_parts})
                        text_parts = []
                    result.append(
                        {
                            "role": "user",
                            "content": [_translate_tool_result(block)],
                        }
                    )
                elif isinstance(block, TextBlock):
                    text_parts.append({"type": "text", "text": block.text})
                elif isinstance(block, ImageBlock):
                    text_parts.append(_translate_image(block))
            if text_parts:
                result.append({"role": "user", "content": text_parts})
    return result


def _translate_assistant_content(
    content: list[Any],
) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, TextBlock):
            parts.append({"type": "text", "text": block.text})
        elif isinstance(block, ToolUseBlock):
            parts.append(
                {
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                }
            )
        elif isinstance(block, ThinkingBlock):
            # Thinking blocks must carry their signature on round-trips or
            # Anthropic will reject the request.
            item: dict[str, Any] = {"type": "thinking", "thinking": block.thinking}
            if block.signature:
                item["signature"] = block.signature
            parts.append(item)
    return parts


def _translate_tool_result(block: ToolResultBlock) -> dict[str, Any]:
    if isinstance(block.content, str):
        content: Any = block.content
    else:
        content = [
            {"type": "text", "text": part.text}
            for part in block.content
            if isinstance(part, TextBlock)
        ]
    item: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": block.tool_use_id,
        "content": content,
    }
    if block.is_error:
        item["is_error"] = True
    return item


def _translate_image(block: ImageBlock) -> dict[str, Any]:
    src = block.source
    if src.get("type") == "url":
        return {"type": "image", "source": {"type": "url", "url": src["url"]}}
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": src.get("media_type", "image/jpeg"),
            "data": src.get("data", ""),
        },
    }


def _translate_tool_choice(tool_choice: Any) -> dict[str, Any]:
    if isinstance(tool_choice, dict):
        # {"name": "tool_name"} → {"type": "tool", "name": "tool_name"}
        return {"type": "tool", "name": tool_choice.get("name", "")}
    mapping = {"auto": "auto", "none": "none", "required": "any"}
    return {"type": mapping.get(str(tool_choice), "auto")}


# ── Stop reason ───────────────────────────────────────────────────────────


def _map_stop_reason(raw: str | None) -> StopReason:
    if raw == "tool_use":
        return "tool_use"
    if raw == "max_tokens":
        return "max_tokens"
    if raw == "refusal":
        return "refusal"
    # "end_turn", "stop_sequence", "pause_turn", None → end_turn
    return "end_turn"


# ── Error mapping ─────────────────────────────────────────────────────────


def _map_anthropic_error(exc: Exception) -> Exception:
    name = exc.__class__.__name__.lower()
    status = getattr(exc, "status_code", None)
    message = str(exc)
    message_lower = message.lower()

    if "authentication" in name or status == 401:
        return AuthError(message)

    if "ratelimit" in name or status == 429:
        retry_after: float | None = None
        resp = getattr(exc, "response", None)
        if resp is not None:
            raw = getattr(resp, "headers", {}).get("retry-after")
            if raw:
                try:
                    retry_after = float(raw)
                except (ValueError, TypeError):
                    pass
        return RateLimitError(message, retry_after_seconds=retry_after)

    if status == 400 and (
        "too long" in message_lower
        or "prompt" in message_lower
        or ("token" in message_lower and "limit" in message_lower)
    ):
        return ContextLengthError(message)

    if isinstance(exc, asyncio.CancelledError):
        return AbortError("aborted")

    retryable = status is None or status >= 500 or status == 408
    return ProviderError(message, status=status, retryable=retryable)
