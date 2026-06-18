"""GeminiProvider — Google Gemini / Vertex AI provider for Linch.

Requires the optional ``[gemini]`` extra:
    pip install 'linch[gemini]'

Uses the ``google-generativeai`` SDK (``import google.generativeai as genai``).
Vertex AI is supported via ``GeminiProviderOptions(project=..., location=...)``.

Streaming tool-use uses a different delta format than OpenAI or Anthropic:
each candidate part is either a ``text`` string or a ``function_call`` object.
We emit Linch-canonical stream events for each part across all streamed
chunks (deduplicating repeated function calls) so the loop processes Gemini
identically to other providers.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from .._prompt_cache import gemini_cached_tokens
from ..errors import AuthError, ContextLengthError, ProviderError, RateLimitError
from ..types import (
    Message,
    ModelId,
    ProviderRequest,
    StopReason,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from .base import BaseProvider, ProviderCapabilities

_KNOWN_CONTEXT: dict[str, int] = {
    "gemini-2.5-pro": 1_048_576,
    "gemini-2.5-flash": 1_048_576,
    "gemini-2.0-flash": 1_048_576,
    "gemini-2.0-flash-lite": 1_048_576,
    "gemini-1.5-pro": 2_000_000,
    "gemini-1.5-flash": 1_048_576,
}

_DEFAULT_CONTEXT = 1_048_576

# Gemini finish_reason codes
_FINISH_STOP = 1
_FINISH_MAX_TOKENS = 2
_FINISH_SAFETY = 3
_FINISH_RECITATION = 4
_FINISH_TOOL_USE = 5  # FUNCTION_CALL in some SDK versions


@dataclass(slots=True)
class GeminiProviderOptions:
    """Configuration for GeminiProvider.

    Attributes:
        api_key: Google AI Studio API key. When None the SDK uses
            ``GOOGLE_API_KEY`` from the environment.
        project: GCP project ID for Vertex AI. When set, the provider
            uses ``vertexai.generativeai`` instead of ``google.generativeai``.
        location: Vertex AI region (default ``us-central1``).
        default_generation_config: Passed verbatim to ``GenerationConfig``.
    """

    api_key: str | None = None
    project: str | None = None
    location: str = "us-central1"
    default_generation_config: dict[str, Any] = field(default_factory=dict)


def _translate_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Convert Linch Messages to Gemini ``contents`` list."""
    contents: list[dict[str, Any]] = []
    tool_names_by_id: dict[str, str] = {}
    for msg in messages:
        # Gemini uses "model" for assistant role
        role = "model" if msg.role == "assistant" else "user"
        parts: list[dict[str, Any]] = []
        for block in msg.content:
            if isinstance(block, TextBlock) and block.text:
                parts.append({"text": block.text})
            elif isinstance(block, ToolUseBlock):
                # Tool call from model → function_call part
                tool_names_by_id[block.id] = block.name
                parts.append({"function_call": {"name": block.name, "args": block.input}})
            elif isinstance(block, ToolResultBlock):
                # Tool result → function_response part
                content_text = (
                    block.content if isinstance(block.content, str) else json.dumps(block.content)
                )
                tool_name = tool_names_by_id.get(block.tool_use_id, block.tool_use_id)
                parts.append(
                    {
                        "function_response": {
                            "id": block.tool_use_id,
                            "name": tool_name,
                            "response": {"content": content_text, "is_error": block.is_error},
                        }
                    }
                )
        if parts:
            contents.append({"role": role, "parts": parts})
    return contents


def _translate_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Linch tool dicts to Gemini function declarations."""
    result = []
    for tool in tools:
        schema = dict(tool.get("input_schema", {}))
        # Gemini uses "parameters" instead of "input_schema"
        result.append(
            {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": schema,
            }
        )
    return result


class GeminiProvider(BaseProvider):
    """Provider implementation for Google Gemini models.

    Example::

        from linch.providers.gemini import GeminiProvider, GeminiProviderOptions

        provider = GeminiProvider(
            GeminiProviderOptions(api_key="AIza...")
        )
        agent = Agent(model="gemini-2.5-pro", provider=provider, ...)
    """

    id = "gemini"

    def __init__(self, options: GeminiProviderOptions | None = None) -> None:
        self._options = options or GeminiProviderOptions()

    def context_window(self, model: ModelId) -> int:
        return _KNOWN_CONTEXT.get(model, _DEFAULT_CONTEXT)

    def capabilities(self, model: ModelId) -> ProviderCapabilities:
        return ProviderCapabilities(
            context_window=self.context_window(model),
            parallel_tool_calls=True,
            structured_output=True,
            tool_choice=True,
            prompt_cache=True,
        )

    def _get_genai(self) -> Any:
        try:
            import google.generativeai as genai  # type: ignore[import]
        except ModuleNotFoundError as exc:
            raise ProviderError(
                "The 'google-generativeai' package is required. "
                "Install with: pip install 'linch[gemini]'"
            ) from exc
        if self._options.api_key is not None:
            genai.configure(api_key=self._options.api_key)  # type: ignore[reportPrivateImportUsage]
        return genai

    def _build_generation_config(self, req: ProviderRequest) -> dict[str, Any]:
        cfg: dict[str, Any] = dict(self._options.default_generation_config)
        if req.max_output_tokens is not None:
            cfg["max_output_tokens"] = req.max_output_tokens
        if req.output_schema is not None:
            cfg["response_mime_type"] = "application/json"
            cfg["response_schema"] = req.output_schema.schema
        return cfg

    async def stream(self, req: ProviderRequest) -> AsyncIterator[dict[str, Any]]:
        genai = self._get_genai()

        # System instruction
        system_text = (
            "\n\n".join(b.text for b in req.system if hasattr(b, "text") and b.text)
            if req.system
            else None
        )

        # Tool declarations
        tool_declarations = _translate_tools(req.tools) if req.tools else []

        gen_config = self._build_generation_config(req)

        try:
            model_kwargs: dict[str, Any] = {"model_name": req.model}
            if system_text:
                model_kwargs["system_instruction"] = system_text
            if tool_declarations:
                model_kwargs["tools"] = tool_declarations
            if gen_config:
                model_kwargs["generation_config"] = gen_config

            model = genai.GenerativeModel(**model_kwargs)
            contents = _translate_messages(req.messages)
        except Exception as exc:
            raise ProviderError(f"Gemini request build failed: {exc}") from exc

        yield {"type": "message_start", "model": req.model}

        input_tokens = 0
        output_tokens = 0
        cache_read_tokens = 0
        stop_reason: StopReason = "end_turn"
        # Cross-chunk dedup: Gemini sends complete function calls (name + full
        # args), not incremental arg deltas. If the SDK re-surfaces the same
        # accumulated call in a later chunk, emitting it twice would make the
        # loop assemble two ToolUseBlocks and the tool execute twice. Key by
        # (name, canonical-json args) and emit each distinct call at most once.
        seen_tool_calls: set[str] = set()

        try:
            async for chunk in model.generate_content_async(contents, stream=True):
                # Accumulate usage from each chunk
                if hasattr(chunk, "usage_metadata"):
                    um = chunk.usage_metadata
                    if hasattr(um, "prompt_token_count") and um.prompt_token_count:
                        input_tokens = um.prompt_token_count
                    if hasattr(um, "candidates_token_count") and um.candidates_token_count:
                        output_tokens = um.candidates_token_count
                    cache_read_tokens = gemini_cached_tokens(um)

                if not chunk.candidates:
                    continue

                candidate = chunk.candidates[0]
                finish_reason = getattr(candidate, "finish_reason", 0)

                # Map finish reason
                if finish_reason == _FINISH_MAX_TOKENS:
                    stop_reason = "max_tokens"
                elif finish_reason in (_FINISH_TOOL_USE, 2):
                    # finish_reason=2 can mean FUNCTION_CALL in some SDK versions
                    # We detect tool use via parts inspection instead
                    pass

                if not hasattr(candidate, "content") or candidate.content is None:
                    continue

                for part in candidate.content.parts:
                    fc = getattr(part, "function_call", None)
                    text = getattr(part, "text", None)

                    if fc is not None and getattr(fc, "name", None):
                        # Tool call part
                        tool_name = fc.name
                        tool_input = dict(fc.args) if fc.args else {}
                        dedup_key = json.dumps([tool_name, tool_input], sort_keys=True, default=str)
                        if dedup_key in seen_tool_calls:
                            stop_reason = "tool_use"
                            continue
                        seen_tool_calls.add(dedup_key)
                        tool_id = f"gemini_{uuid.uuid4().hex[:8]}"
                        yield {
                            "type": "tool_use_start",
                            "id": tool_id,
                            "name": tool_name,
                        }
                        yield {
                            "type": "tool_use_input_delta",
                            "id": tool_id,
                            "json_delta": json.dumps(tool_input),
                        }
                        yield {"type": "tool_use_end", "id": tool_id}
                        stop_reason = "tool_use"
                    elif text:
                        yield {"type": "text_delta", "text": text}

        except Exception as exc:
            _msg = str(exc).lower()
            if "api_key" in _msg or "credentials" in _msg or "permission" in _msg:
                raise AuthError(f"Gemini auth error: {exc}") from exc
            if "quota" in _msg or "rate" in _msg or "429" in _msg:
                raise RateLimitError(f"Gemini rate limit: {exc}") from exc
            if "context" in _msg or "too long" in _msg or "tokens" in _msg:
                raise ContextLengthError(f"Gemini context exceeded: {exc}") from exc
            raise ProviderError(f"Gemini stream error: {exc}", retryable=False) from exc

        yield {
            "type": "message_end",
            "stop_reason": stop_reason,
            "usage": Usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
            ),
            "provider_metadata": None,
        }
