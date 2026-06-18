from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal

from ._http_errors import (
    error_message,
    error_status,
    is_prompt_length_error,
    retry_after_seconds,
)
from ._prompt_cache import openai_responses_cached_tokens
from .errors import AbortError, AuthError, ContextLengthError, ProviderError, RateLimitError
from .types import (
    ImageBlock,
    Message,
    ProviderRequest,
    StopReason,
    SystemBlock,
    TextBlock,
    Usage,
)

OpenAIReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh"]
OpenAIReasoningSummary = Literal["auto", "concise", "detailed"]


@dataclass(slots=True)
class OpenAIOptions:
    api_key: str | None = None
    base_url: str | None = None
    default_headers: dict[str, str] | None = None


@dataclass(slots=True)
class OpenAIReasoning:
    effort: OpenAIReasoningEffort | None = None
    summary: OpenAIReasoningSummary | None = None
    previous_response_id: Literal["auto", "disabled"] = "auto"
    encrypted_content: bool = False


KNOWN_CONTEXT_WINDOWS = {
    "gpt-5": 400_000,
    "gpt-4.1": 1_000_000,
    "gpt-4o": 128_000,
    "o1": 200_000,
}
DEFAULT_CONTEXT_WINDOW = 128_000


def context_window(model: str) -> int:
    return KNOWN_CONTEXT_WINDOWS.get(model, DEFAULT_CONTEXT_WINDOW)


def translate_instructions(blocks: list[SystemBlock]) -> str:
    return "\n\n".join(block.text for block in blocks)


def translate_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool["input_schema"],
        }
        for tool in tools
    ]


def image_to_url(source: dict[str, str]) -> str:
    if source.get("type") == "url":
        return source["url"]
    return f"data:{source['media_type']};base64,{source['data']}"


def tool_result_to_output(
    content: str | list[TextBlock | ImageBlock],
) -> str | list[dict[str, str]]:
    if isinstance(content, str):
        return content
    if not any(isinstance(part, ImageBlock) for part in content):
        return "\n".join(getattr(part, "text", "") for part in content)
    output: list[dict[str, str]] = []
    for part in content:
        if isinstance(part, ImageBlock):
            output.append({"type": "input_image", "image_url": image_to_url(part.source)})
        else:
            output.append({"type": "input_text", "text": part.text})
    return output


def translate_input(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "assistant":
            raw = (message.provider_metadata or {}).get("openai_responses", {}).get("output_items")
            if raw:
                out.extend(raw)
                continue
            parts: list[dict[str, Any]] = []
            calls: list[dict[str, Any]] = []
            for block in message.content:
                if block.type == "text":
                    parts.append({"type": "output_text", "text": block.text})
                elif block.type == "tool_use":
                    calls.append(
                        {
                            "type": "function_call",
                            "call_id": block.id,
                            "name": block.name,
                            "arguments": json.dumps(block.input),
                        }
                    )
            if parts:
                out.append({"type": "message", "role": "assistant", "content": parts})
            out.extend(calls)
            continue

        parts = []
        for block in message.content:
            if block.type == "tool_result":
                out.append(
                    {
                        "type": "function_call_output",
                        "call_id": block.tool_use_id,
                        "output": tool_result_to_output(block.content),
                    }
                )
            elif block.type == "text":
                parts.append({"type": "input_text", "text": block.text})
            elif block.type == "image":
                parts.append({"type": "input_image", "image_url": image_to_url(block.source)})
        if parts:
            out.append({"type": "message", "role": "user", "content": parts})
    return out


def previous_response(messages: list[Message]) -> tuple[str, int] | None:
    for index in range(len(messages) - 1, -1, -1):
        metadata = messages[index].provider_metadata or {}
        response_id = metadata.get("openai_responses", {}).get("response_id")
        if isinstance(response_id, str):
            return response_id, index
    return None


def build_payload(req: ProviderRequest, reasoning: OpenAIReasoning | None = None) -> dict[str, Any]:
    previous = (
        previous_response(req.messages)
        if reasoning is None or reasoning.previous_response_id == "auto"
        else None
    )
    input_messages = req.messages[previous[1] + 1 :] if previous else req.messages
    payload: dict[str, Any] = {
        "model": req.model,
        "input": translate_input(input_messages),
        "stream": True,
    }
    instructions = translate_instructions(req.system)
    if instructions:
        payload["instructions"] = instructions
    if req.tools:
        payload["tools"] = translate_tools(req.tools)
    if req.max_output_tokens is not None:
        payload["max_output_tokens"] = req.max_output_tokens
    if req.temperature is not None:
        payload["temperature"] = req.temperature
    if previous:
        payload["previous_response_id"] = previous[0]
    # Reasoning effort: per-request wins over constructor-level config
    effective_effort = req.effort or (reasoning.effort if reasoning is not None else None)
    effective_summary = reasoning.summary if reasoning is not None else None
    if effective_effort is not None or effective_summary is not None:
        payload["reasoning"] = {}
        if effective_effort is not None:
            payload["reasoning"]["effort"] = effective_effort
        if effective_summary is not None:
            payload["reasoning"]["summary"] = effective_summary
    if reasoning is not None and reasoning.encrypted_content:
        payload["include"] = ["reasoning.encrypted_content"]
    # Structured output (Responses API uses text.format)
    if req.output_schema is not None:
        payload["text"] = {
            "format": {
                "type": "json_schema",
                "name": req.output_schema.name,
                "strict": req.output_schema.strict,
                "schema": req.output_schema.schema,
            }
        }
    # Tool choice
    if req.tool_choice is not None:
        if isinstance(req.tool_choice, dict):
            payload["tool_choice"] = {
                "type": "function",
                "name": req.tool_choice.get("name", ""),
            }
        else:
            payload["tool_choice"] = req.tool_choice
    return payload


def derive_stop_reason(
    status: str | None,
    has_function_call: bool,
    incomplete_reason: str | None,
) -> StopReason:
    if status == "completed":
        return "tool_use" if has_function_call else "end_turn"
    if status == "incomplete":
        if incomplete_reason == "max_output_tokens":
            return "max_tokens"
        if incomplete_reason == "content_filter":
            return "refusal"
    return "error"


def build_usage(raw: dict[str, Any] | None) -> Usage:
    return Usage(
        input_tokens=int((raw or {}).get("input_tokens") or 0),
        output_tokens=int((raw or {}).get("output_tokens") or 0),
        cache_read_tokens=openai_responses_cached_tokens(raw),
    )


def map_openai_error(err: Exception) -> Exception:
    name = err.__class__.__name__.lower()
    status = error_status(err)
    message = error_message(err)
    if "authentication" in name or status == 401:
        return AuthError(message)
    if "ratelimit" in name or status == 429:
        return RateLimitError(message, retry_after_seconds=retry_after_seconds(err))
    # Reclassify a prompt-length error when the status is a bad-request (400) or
    # unknown (None). OpenAI-compatible/local endpoints (DeepSeek, llama.cpp)
    # often raise context overflows without an integer status_code; gating only
    # on 400 let those fall through to the retryable fallback — a retry storm.
    if (status == 400 or status is None) and is_prompt_length_error(err):
        return ContextLengthError(message)
    if isinstance(err, asyncio.CancelledError):
        return AbortError("aborted")
    return ProviderError(
        message,
        status=status,
        retryable=status is None or status >= 500 or status == 408,
    )


class OpenAIResponsesClient:
    def __init__(
        self,
        options: OpenAIOptions | None = None,
        reasoning: OpenAIReasoning | None = None,
    ):
        options = options or OpenAIOptions()
        self._kwargs: dict[str, Any] = {}
        if options.api_key is not None:
            self._kwargs["api_key"] = options.api_key
        if options.base_url is not None:
            self._kwargs["base_url"] = options.base_url
        if options.default_headers is not None:
            self._kwargs["default_headers"] = options.default_headers
        self.client: Any | None = None
        self.reasoning = reasoning

    async def stream(self, req: ProviderRequest) -> AsyncIterator[dict[str, Any]]:
        if self.client is None:
            try:
                from openai import AsyncOpenAI
            except ModuleNotFoundError as exc:
                raise ProviderError(
                    "The 'openai' package is required for live OpenAI calls. "
                    "Install linch with its runtime dependencies."
                ) from exc
            self.client = AsyncOpenAI(**self._kwargs)
        assert self.client is not None
        client = self.client
        payload = build_payload(req, self.reasoning)
        try:
            stream = await client.responses.create(**payload)
            async for event in stream:
                yield event.model_dump() if hasattr(event, "model_dump") else dict(event)
        except asyncio.CancelledError as exc:
            raise AbortError("aborted") from exc
        except Exception as exc:
            if getattr(req.signal, "aborted", False):
                raise AbortError("aborted") from exc
            raise map_openai_error(exc) from exc


async def map_wire_events(
    wire: AsyncIterator[dict[str, Any]],
    model: str,
) -> AsyncIterator[dict[str, Any]]:
    yield {"type": "message_start", "model": model}
    item_to_call_id: dict[str, str] = {}
    has_function_call = False
    stop_reason: StopReason = "end_turn"
    usage = Usage()
    response_id: str | None = None
    output_items: list[dict[str, Any]] | None = None

    async for ev in wire:
        typ = ev.get("type")
        if typ == "response.output_item.added":
            item = ev.get("item") or {}
            if item.get("type") == "function_call" and item.get("id") and item.get("call_id"):
                item_to_call_id[item["id"]] = item["call_id"]
                has_function_call = True
                yield {"type": "tool_use_start", "id": item["call_id"], "name": item["name"]}
        elif typ == "response.output_text.delta" and ev.get("delta"):
            yield {"type": "text_delta", "text": ev["delta"]}
        elif typ in {
            "response.reasoning_summary_text.delta",
            "response.reasoning.delta",
        } and ev.get("delta"):
            yield {"type": "thinking_delta", "text": ev["delta"]}
        elif typ == "response.function_call_arguments.delta":
            call_id = item_to_call_id.get(str(ev.get("item_id")))
            if call_id and ev.get("delta") is not None:
                yield {"type": "tool_use_input_delta", "id": call_id, "json_delta": ev["delta"]}
        elif typ == "response.output_item.done":
            item = ev.get("item") or {}
            if item.get("type") == "function_call":
                call_id = item_to_call_id.pop(str(item.get("id")), None)
                if call_id:
                    yield {"type": "tool_use_end", "id": call_id}
        elif typ in {"response.completed", "response.failed", "response.incomplete"}:
            response = ev.get("response") or {}
            response_id = response.get("id")
            output_items = response.get("output")
            stop_reason = derive_stop_reason(
                response.get("status"),
                has_function_call,
                (response.get("incomplete_details") or {}).get("reason"),
            )
            usage = build_usage(response.get("usage"))

    metadata = None
    if response_id or output_items:
        metadata = {
            "openai_responses": {
                **({"response_id": response_id} if response_id else {}),
                **({"output_items": output_items} if output_items else {}),
            }
        }
    yield {
        "type": "message_end",
        "stop_reason": stop_reason,
        "usage": usage,
        "provider_metadata": metadata,
    }
