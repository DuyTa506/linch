from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from agent_kit.errors import ProviderError
from agent_kit.openai_responses import map_openai_error
from agent_kit.providers.base import BaseProvider
from agent_kit.types import (
    ImageBlock,
    ModelId,
    ProviderRequest,
    StopReason,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)

_KNOWN_CONTEXT = {
    "gpt-5": 400_000,
    "gpt-4.1": 1_000_000,
    "gpt-4o": 128_000,
    "o1": 200_000,
}


def _image_to_url(source: dict[str, str]) -> str:
    if source.get("type") == "url":
        return source.get("url", "")
    media_type = source.get("media_type", "application/octet-stream")
    data = source.get("data", "")
    return f"data:{media_type};base64,{data}"


@dataclass(slots=True)
class OpenAIChatProviderOptions:
    api_key: str | None = None
    base_url: str | None = None
    default_headers: dict[str, str] | None = None


class OpenAIChatCompletionsProvider(BaseProvider):
    id = "openai-chat"

    def __init__(self, options: OpenAIChatProviderOptions | None = None) -> None:
        self._options = options or OpenAIChatProviderOptions()
        self._client: Any | None = None

    def context_window(self, model: ModelId) -> int:
        return _KNOWN_CONTEXT.get(model, 128_000)

    async def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from openai import AsyncOpenAI
        except ModuleNotFoundError as exc:
            raise ProviderError(
                "The 'openai' package is required for OpenAI chat provider."
            ) from exc
        kwargs: dict[str, Any] = {}
        if self._options.api_key is not None:
            kwargs["api_key"] = self._options.api_key
        if self._options.base_url is not None:
            kwargs["base_url"] = self._options.base_url
        if self._options.default_headers is not None:
            kwargs["default_headers"] = self._options.default_headers
        self._client = AsyncOpenAI(**kwargs)
        return self._client

    async def stream(self, req: ProviderRequest) -> AsyncIterator[dict[str, object]]:
        client = await self._get_client()
        payload = _build_chat_payload(req)
        yield {"type": "message_start", "model": req.model}
        tool_input: dict[str, str] = {}
        tool_meta: dict[str, tuple[str, str]] = {}
        usage = Usage()
        stop_reason: StopReason = "end_turn"
        try:
            stream = await client.chat.completions.create(**payload)
            async for chunk in stream:
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                delta = choices[0].delta
                if getattr(delta, "content", None):
                    yield {"type": "text_delta", "text": str(delta.content)}
                for tc in getattr(delta, "tool_calls", None) or []:
                    tid = str(getattr(tc, "id", "") or f"tool_{tc.index}")
                    fn = getattr(tc, "function", None)
                    fallback_name = tool_meta.get(tid, (tid, ""))[1]
                    fname = str(getattr(fn, "name", "") or fallback_name or "Tool")
                    if tid not in tool_meta:
                        tool_meta[tid] = (tid, fname)
                        tool_input[tid] = ""
                        yield {"type": "tool_use_start", "id": tid, "name": fname}
                    args_delta = str(getattr(fn, "arguments", "") or "")
                    if args_delta:
                        tool_input[tid] += args_delta
                        yield {"type": "tool_use_input_delta", "id": tid, "json_delta": args_delta}
                finish_reason = choices[0].finish_reason
                if finish_reason == "tool_calls":
                    stop_reason = "tool_use"
                    for tid in list(tool_meta):
                        yield {"type": "tool_use_end", "id": tid}
                elif finish_reason == "length":
                    stop_reason = "max_tokens"
                elif finish_reason == "content_filter":
                    stop_reason = "refusal"
                elif finish_reason == "stop":
                    stop_reason = "end_turn"
                cu = getattr(chunk, "usage", None)
                if cu is not None:
                    usage = Usage(
                        input_tokens=int(getattr(cu, "prompt_tokens", 0) or 0),
                        output_tokens=int(getattr(cu, "completion_tokens", 0) or 0),
                    )
        except Exception as exc:
            raise map_openai_error(exc) from exc
        yield {"type": "message_end", "stop_reason": stop_reason, "usage": usage}


def _build_chat_payload(req: ProviderRequest) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    if req.system:
        messages.append(
            {"role": "system", "content": "\n\n".join(block.text for block in req.system)}
        )
    for message in req.messages:
        if message.role == "assistant":
            text_blocks = [b.text for b in message.content if isinstance(b, TextBlock)]
            tool_calls = []
            for b in message.content:
                if isinstance(b, ToolUseBlock):
                    tool_calls.append(
                        {
                            "id": b.id,
                            "type": "function",
                            "function": {"name": b.name, "arguments": json.dumps(b.input)},
                        }
                    )
            entry: dict[str, Any] = {
                "role": "assistant",
                "content": "".join(text_blocks) if text_blocks else None,
            }
            if tool_calls:
                entry["tool_calls"] = tool_calls
            messages.append(entry)
            continue

        # user
        text_parts: list[dict[str, Any]] = []
        for block in message.content:
            if isinstance(block, ToolResultBlock):
                if isinstance(block.content, str):
                    content = block.content
                else:
                    content = "\n".join(
                        part.text for part in block.content if isinstance(part, TextBlock)
                    )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": block.tool_use_id,
                        "content": content or "[tool result]",
                    }
                )
            elif isinstance(block, TextBlock):
                text_parts.append({"type": "text", "text": block.text})
            elif isinstance(block, ImageBlock):
                text_parts.append(
                    {"type": "image_url", "image_url": {"url": _image_to_url(block.source)}}
                )
        if text_parts:
            messages.append({"role": "user", "content": text_parts})

    tools = []
    for schema in req.tools:
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": schema["name"],
                    "description": schema["description"],
                    "parameters": schema["input_schema"],
                },
            }
        )

    payload: dict[str, Any] = {
        "model": req.model,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if tools:
        payload["tools"] = tools
    if req.max_output_tokens is not None:
        payload["max_tokens"] = req.max_output_tokens
    if req.temperature is not None:
        payload["temperature"] = req.temperature
    if req.stop_sequences:
        payload["stop"] = req.stop_sequences
    # Structured output (JSON Schema)
    if req.output_schema is not None:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": req.output_schema.name,
                "strict": req.output_schema.strict,
                "schema": req.output_schema.schema,
            },
        }
    # Tool choice
    if req.tool_choice is not None:
        if isinstance(req.tool_choice, dict):
            payload["tool_choice"] = {
                "type": "function",
                "function": {"name": req.tool_choice.get("name", "")},
            }
        else:
            payload["tool_choice"] = req.tool_choice
    return payload
