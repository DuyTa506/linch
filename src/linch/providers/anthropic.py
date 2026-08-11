from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal, cast
from urllib.parse import urlparse

from linch._client_lifecycle import aclose_client, loop_changed, running_loop
from linch._http_errors import (
    error_message,
    error_status,
    is_prompt_length_error,
    retry_after_seconds,
)
from linch._prompt_cache import (
    ANTHROPIC_PROMPT_CACHE,
    mark_last_cacheable_message_content,
    mark_system_cache_breakpoint,
)
from linch.errors import (
    AbortError,
    AuthError,
    ContextLengthError,
    ProviderError,
    RateLimitError,
)
from linch.providers.base import BaseProvider, ProviderCapabilities, full_capabilities
from linch.types import (
    ImageBlock,
    Message,
    ModelId,
    ProviderRequest,
    RedactedThinkingBlock,
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

AnthropicAPIMode = Literal["auto", "native", "compatible"]


@dataclass(slots=True)
class AnthropicProviderOptions:
    api_key: str | None = None
    base_url: str | None = None
    default_headers: dict[str, str] | None = None
    # Native Messages API configuration, for example
    # {"type": "enabled", "budget_tokens": 4096}, {"type": "adaptive"},
    # or {"type": "disabled"}. ``None`` deliberately preserves the model's
    # provider-default thinking behavior.
    thinking: dict[str, Any] | None = None
    # ``native`` uses Claude Messages features such as
    # ``output_config.format``. ``compatible`` retains the generated final
    # schema-tool fallback for Anthropic-shaped third-party APIs. ``auto`` is
    # native only for the direct Anthropic endpoint (or its SDK default), which
    # keeps unknown proxies on the conservative compatible path.
    api_mode: AnthropicAPIMode = "auto"
    effort: str | None = None


class AnthropicProvider(BaseProvider):
    id = "anthropic"

    def __init__(self, options: AnthropicProviderOptions | None = None) -> None:
        self._options = options or AnthropicProviderOptions()
        self._client: Any | None = None
        self._client_loop: Any | None = None

    def context_window(self, model: ModelId) -> int:
        return _KNOWN_CONTEXT.get(model, 200_000)

    def capabilities(self, model: ModelId) -> ProviderCapabilities:
        return full_capabilities(
            self.context_window(model),
            structured_output_terminal_tool=_effective_api_mode(self._options) == "compatible",
        )

    async def _get_client(self) -> Any:
        if self._client is not None and loop_changed(self._client_loop):
            # The old client's transport belongs to a loop that is gone; its
            # close is async and cannot be awaited from here, so drop it and
            # let GC reclaim the socket rather than raising on every call.
            self._client = None
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
        self._client_loop = running_loop()
        return self._client

    async def aclose(self) -> None:
        client = self._client
        self._client = None
        self._client_loop = None
        if client is None:
            return
        await aclose_client(client)

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
                    elif cb.type == "redacted_thinking":
                        data = getattr(cb, "data", "")
                        yield {"type": "redacted_thinking", "data": str(data)}
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
            # Only a cooperative `signal.abort()` maps to AbortError. A CancelledError
            # with no matching signal comes from an external cancel (e.g. the caller's
            # `asyncio.wait_for(session.run(...), timeout=N)` firing) and must propagate
            # unchanged, or `wait_for` silently swallows the timeout instead of raising.
            if getattr(req.signal, "aborted", False):
                raise AbortError("aborted") from exc
            raise
        except Exception as exc:
            if getattr(req.signal, "aborted", False):
                raise AbortError("aborted") from exc
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
    api_mode = _effective_api_mode(opts)
    # Resolve this before structured-output handling: compatible APIs cannot
    # combine a forced ``tool_choice`` with active extended/adaptive thinking.
    thinking_cfg = _effective_thinking(req, opts)
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
    payload["messages"] = _translate_messages(req.messages, req.cache_prompt, req.cache_ttl)

    # ── Tools ─────────────────────────────────────────────────────────────
    if req.tools:
        payload["tools"] = _translate_tools(req.tools)

    # ── Optional scalar fields ────────────────────────────────────────────
    if req.temperature is not None:
        payload["temperature"] = req.temperature
    if req.stop_sequences:
        payload["stop_sequences"] = req.stop_sequences

    # Native Claude JSON output and provider effort share ``output_config``.
    # DeepSeek's Anthropic-compatible API supports only ``effort`` here, so it
    # must never receive the native ``format`` object.
    output_config: dict[str, Any] = {}
    effort = _effective_effort(req, opts)
    if effort is not None:
        output_config["effort"] = effort
    if req.output_schema is not None and api_mode == "native":
        output_config["format"] = {
            "type": "json_schema",
            "schema": req.output_schema.schema,
        }
    if output_config:
        payload["output_config"] = output_config

    # ── Tool choice ───────────────────────────────────────────────────────
    if req.tool_choice is not None:
        payload["tool_choice"] = _translate_tool_choice(req.tool_choice)

    # ── Output schema (conservative compatible fallback) ─────────────────
    # Native Claude uses ``output_config.format`` above. A third-party API that
    # only accepts Anthropic-shaped messages may not implement it, so retain
    # the generated terminal-tool fallback in explicitly compatible mode.
    if req.output_schema is not None and api_mode == "compatible":
        schema_tool: dict[str, Any] = {
            "name": req.output_schema.name,
            "description": req.output_schema.description or "",
            "input_schema": req.output_schema.schema,
        }
        existing_tools = list(payload.get("tools", []))
        payload["tools"] = existing_tools + [schema_tool]
        # Force the schema tool only when it is the sole tool available and
        # thinking is disabled. Anthropic only permits ``auto`` or ``none``
        # tool choice with active extended/adaptive thinking. An explicit
        # caller choice always wins.
        if not existing_tools and req.tool_choice is None:
            if _thinking_is_active(thinking_cfg):
                payload["tool_choice"] = {"type": "auto"}
            else:
                payload["tool_choice"] = {"type": "tool", "name": req.output_schema.name}

    # ── Thinking ──────────────────────────────────────────────────────────
    if thinking_cfg is not None:
        payload["thinking"] = thinking_cfg

    return payload


def _effective_thinking(
    req: ProviderRequest,
    opts: AnthropicProviderOptions,
) -> dict[str, Any] | None:
    """Return a copied native thinking config, with a per-run override winning."""

    configured = req.thinking if req.thinking is not None else opts.thinking
    return dict(configured) if configured else None


def _effective_effort(req: ProviderRequest, opts: AnthropicProviderOptions) -> str | None:
    """Return a run-level effort override or the provider default."""

    return req.effort if req.effort is not None else opts.effort


def _effective_api_mode(opts: AnthropicProviderOptions) -> Literal["native", "compatible"]:
    """Select native Claude features only for the direct Messages endpoint.

    Anthropic-compatible endpoints intentionally default to the conservative
    path: sharing a request shape does not imply support for Claude's newer
    JSON-schema or thinking semantics. Callers that own a compatible proxy can
    opt into ``api_mode=\"native\"`` explicitly after verifying it.
    """

    if opts.api_mode == "native":
        return "native"
    if opts.api_mode == "compatible":
        return "compatible"
    if opts.base_url is None:
        return "native"
    host = (urlparse(opts.base_url).hostname or "").lower()
    return "native" if host == "api.anthropic.com" else "compatible"


def _thinking_is_active(thinking: dict[str, Any] | None) -> bool:
    """Whether Anthropic's tool-choice restrictions apply to this request."""

    return thinking is not None and thinking.get("type") != "disabled"


def _translate_system(
    blocks: list[SystemBlock],
    cache: bool | None,
    ttl: str | None,
) -> list[dict[str, Any]]:
    """Convert system blocks to the Anthropic system array, with optional caching.

    The cache breakpoint is placed on the *last* ``cacheable=True`` block in the
    array (ROADMAP 3.2), so the cached prefix extends as far as possible. A
    ``cacheable=False`` block placed *after* the last static block (a volatile
    trailing section) therefore sits outside the cached prefix and never
    invalidates it. When every block is static this is simply the last block —
    byte-identical to the legacy "cache the last block" behavior for the
    all-static prompts the agent builds today.

    Note the tradeoff: a ``cacheable=False`` block that appears *before* the last
    cacheable block is still inside the cached prefix, so changing it invalidates
    the cache. The agent keeps per-turn dynamic content (env/date) in user
    messages, not the system array, so this does not arise in practice.
    """
    if not blocks:
        return []
    result: list[dict[str, Any]] = [{"type": "text", "text": block.text} for block in blocks]
    if not cache:
        return result
    mark_system_cache_breakpoint(
        result,
        blocks,
        cache=cache,
        ttl=ttl,
        wire=ANTHROPIC_PROMPT_CACHE,
    )
    return result


def _translate_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Linch tool schemas to Anthropic ToolParam dicts."""
    result: list[dict[str, Any]] = []
    for tool in tools:
        item: dict[str, Any] = {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "input_schema": tool["input_schema"],
        }
        result.append(item)
    return result


def _translate_messages(
    messages: list[Message],
    cache: bool | None = None,
    ttl: str | None = None,
) -> list[dict[str, Any]]:
    """Convert internal Message objects to Anthropic message dicts."""
    result: list[dict[str, Any]] = []
    last_user_index = next(
        (i for i in range(len(messages) - 1, -1, -1) if messages[i].role == "user"),
        None,
    )
    for index, msg in enumerate(messages):
        emitted_for_message: list[dict[str, Any]] = []
        if msg.role == "assistant":
            content = _translate_assistant_content(msg.content)
            if content:
                result.append({"role": "assistant", "content": content})
        else:
            # Anthropic requires every result for one assistant tool-use turn
            # in the immediately following *single* user message. In particular,
            # splitting parallel results into one user message each leaves the
            # later tool-use IDs unmatched and the API rejects the request.
            # Tool-result blocks must lead a mixed user message, so keep their
            # order and place any ordinary user content after them.
            tool_result_parts: list[dict[str, Any]] = []
            content_parts: list[dict[str, Any]] = []
            for block in msg.content:
                if isinstance(block, ToolResultBlock):
                    tool_result_parts.append(_translate_tool_result(block))
                elif isinstance(block, TextBlock):
                    content_parts.append({"type": "text", "text": block.text})
                elif isinstance(block, ImageBlock):
                    content_parts.append(_translate_image(block))
            content = tool_result_parts + content_parts
            if content:
                emitted_for_message.append({"role": "user", "content": content})
            if cache and index == last_user_index:
                mark_last_cacheable_message_content(
                    emitted_for_message,
                    cache=cache,
                    ttl=ttl,
                    wire=ANTHROPIC_PROMPT_CACHE,
                    content_types={"text", "image", "tool_result"},
                )
            result.extend(emitted_for_message)
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
        elif isinstance(block, RedactedThinkingBlock):
            parts.append({"type": "redacted_thinking", "data": block.data})
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
    status = error_status(exc)
    message = error_message(exc)

    if "authentication" in name or status == 401:
        return AuthError(message)

    if "ratelimit" in name or status == 429:
        return RateLimitError(message, retry_after_seconds=retry_after_seconds(exc))

    # Reclassify a prompt-length error when the status is a bad-request (400) or
    # unknown (None). OpenAI-compatible/local endpoints often raise context
    # overflows without an integer status_code; gating only on 400 let those
    # fall through to the retryable fallback below — an unrecoverable retry storm.
    if (status == 400 or status is None) and is_prompt_length_error(exc):
        return ContextLengthError(message)

    if isinstance(exc, asyncio.CancelledError):
        return AbortError("aborted")

    retryable = status is None or status >= 500 or status == 408
    return ProviderError(message, status=status, retryable=retryable)
