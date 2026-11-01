"""Google Gemini / Vertex AI via the optional ``google-genai`` SDK.

SDK objects stay inside this adapter; the loop receives normalized Linch events.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any

from .._client_lifecycle import loop_changed, running_loop
from .._http_errors import retry_after_seconds
from ..errors import AuthError, ContextLengthError, LinchError, ProviderError, RateLimitError
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
from .base import BaseProvider, ProviderCapabilities, full_capabilities

_KNOWN_CONTEXT: dict[str, int] = {
    "gemini-2.5-pro": 1_048_576,
    "gemini-2.5-flash": 1_048_576,
    "gemini-2.0-flash": 1_048_576,
    "gemini-2.0-flash-lite": 1_048_576,
    "gemini-1.5-pro": 2_000_000,
    "gemini-1.5-flash": 1_048_576,
}


@dataclass(slots=True)
class GeminiProviderOptions:
    """SDK credentials and native ``GenerateContentConfig`` defaults.

    ``api_key=None`` uses SDK environment discovery. Setting ``project`` selects
    Vertex AI with application default credentials and the given ``location``.
    ``default_generation_config`` accepts native options, including ``thinking_config``.

    Args:
        api_key: Optional Gemini API key used when Vertex AI is not selected.
        project: Optional Google Cloud project; setting it selects Vertex AI.
        location: Vertex AI location used with ``project``.
        default_generation_config: Native generation settings applied to requests.

    Attributes:
        default_generation_config: A mutable mapping copied and extended with
            request-level generation and thinking settings.
    """

    api_key: str | None = None
    project: str | None = None
    location: str = "us-central1"
    default_generation_config: dict[str, Any] = field(default_factory=dict)


def _translate_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Convert history, replaying signed model parts retained in message metadata."""
    contents: list[dict[str, Any]] = []
    tool_names_by_id: dict[str, str] = {}
    for msg in messages:
        role = "model" if msg.role == "assistant" else "user"
        parts: list[dict[str, Any]] = []
        for block in msg.content:
            if isinstance(block, TextBlock) and block.text:
                parts.append({"text": block.text})
            elif isinstance(block, ToolUseBlock):
                tool_names_by_id[block.id] = block.name
                parts.append(
                    {"function_call": {"id": block.id, "name": block.name, "args": block.input}}
                )
            elif isinstance(block, ToolResultBlock):
                content_text = (
                    block.content if isinstance(block.content, str) else json.dumps(block.content)
                )
                parts.append(
                    {
                        "function_response": {
                            "id": block.tool_use_id,
                            "name": tool_names_by_id.get(block.tool_use_id, block.tool_use_id),
                            "response": {"content": content_text, "is_error": block.is_error},
                        }
                    }
                )
        if role == "model" and msg.provider_metadata:
            parts = msg.provider_metadata.get("gemini_parts", parts)
        if parts:
            contents.append({"role": role, "parts": parts})
    return contents


def _translate_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "name": t["name"],
            "description": t.get("description", ""),
            "parameters_json_schema": t.get("input_schema", {}),
        }
        for t in tools
    ]


def _translate_tool_choice(tool_choice: Any) -> dict[str, Any] | None:
    if tool_choice is None:
        return None
    if isinstance(tool_choice, dict):
        config: dict[str, Any] = {"mode": "ANY"}
        if tool_choice.get("name"):
            config["allowed_function_names"] = [tool_choice["name"]]
    else:
        config = {
            "mode": {"auto": "AUTO", "none": "NONE", "required": "ANY", "any": "ANY"}.get(
                str(tool_choice), "AUTO"
            )
        }
    return {"function_calling_config": config}


def _map_error(exc: Exception) -> LinchError:
    from httpx import TransportError

    code = getattr(exc, "code", None)
    msg = str(exc).lower()
    if code in (401, 403) or (
        code in (None, 400)
        and any(s in msg for s in ("api_key", "api key", "credentials", "permission"))
    ):
        return AuthError(f"Gemini auth error: {exc}")
    if code == 429 or (code is None and any(s in msg for s in ("quota", "rate limit", "429"))):
        return RateLimitError(
            f"Gemini rate limit: {exc}", retry_after_seconds=retry_after_seconds(exc)
        )
    if code in (None, 400, 413) and (
        "context" in msg
        or "too long" in msg
        or (
            "token" in msg
            and ("input" in msg or "prompt" in msg)
            and any(s in msg for s in ("exceed", "maximum", "limit"))
        )
    ):
        return ContextLengthError(f"Gemini context exceeded: {exc}")
    return ProviderError(
        f"Gemini request failed: {exc}",
        status=code,
        retryable=code == 408
        or (isinstance(code, int) and code >= 500)
        or isinstance(exc, (OSError, TransportError)),
    )


class GeminiProvider(BaseProvider):
    """Async Gemini provider; client configuration is isolated per instance.

    Args:
        options: Optional Gemini or Vertex AI credentials and generation defaults.
    """

    id = "gemini"

    def __init__(self, options: GeminiProviderOptions | None = None) -> None:
        """Create an isolated Gemini provider client configuration.

        Args:
            options: Gemini or Vertex AI credentials and generation defaults.
        """
        self._options = options or GeminiProviderOptions()
        self._client: Any = None
        self._client_loop: Any = None

    def context_window(self, model: ModelId) -> int:
        """Return Gemini's configured context capacity for a model.

        Args:
            model: Gemini model identifier.

        Returns:
            The known model capacity, or the adapter fallback for unknown models.
        """
        return _KNOWN_CONTEXT.get(model, 1_048_576)

    def capabilities(self, model: ModelId) -> ProviderCapabilities:
        """Return capabilities for the selected Gemini model.

        Args:
            model: Gemini model identifier.

        Returns:
            Capability flags advertised by the Gemini adapter.
        """
        return full_capabilities(self.context_window(model))

    def _get_client(self) -> Any:
        if self._client is not None and loop_changed(self._client_loop):
            self._client = None
        if self._client is None:
            try:
                import google.genai as genai
            except ImportError as exc:
                raise ProviderError(
                    "The 'google-genai' package is required. "
                    "Install with: pip install 'linch[gemini]'"
                ) from exc
            kwargs: dict[str, Any] = {}
            if self._options.project is not None:
                kwargs.update(
                    vertexai=True, project=self._options.project, location=self._options.location
                )
            elif self._options.api_key is not None:
                kwargs["api_key"] = self._options.api_key
            self._client = genai.Client(**kwargs)
            self._client_loop = running_loop()
        return self._client

    async def aclose(self) -> None:
        """Close the owned Gemini clients, if initialized."""
        client, self._client = self._client, None
        self._client_loop = None
        if client is not None:
            try:
                await client.aio.aclose()
            finally:
                await asyncio.to_thread(client.close)

    def _build_generation_config(self, req: ProviderRequest) -> dict[str, Any]:
        cfg = dict(self._options.default_generation_config)
        for key in ("max_output_tokens", "temperature", "stop_sequences"):
            if (value := getattr(req, key)) is not None:
                cfg[key] = value
        if req.thinking is not None:
            cfg["thinking_config"] = req.thinking
            mode = req.thinking.get("type")
            if mode in ("enabled", "disabled", "adaptive"):
                budget = {"disabled": 0, "adaptive": -1}.get(
                    mode, req.thinking.get("budget_tokens", -1)
                )
                cfg["thinking_config"] = {"thinking_budget": budget}
                if (display := req.thinking.get("display")) is not None:
                    cfg["thinking_config"]["include_thoughts"] = display != "omitted"
        if req.output_schema is not None:
            cfg.pop("response_schema", None)
            cfg.update(
                response_mime_type="application/json", response_json_schema=req.output_schema.schema
            )
        return cfg

    async def stream(self, req: ProviderRequest) -> AsyncIterator[dict[str, Any]]:
        """Yield normalized events for one Gemini request.

        Args:
            req: Request to send through the Google GenAI client.

        Returns:
            An async iterator of normalized message, thinking, tool, and usage events.
        """
        try:
            client = self._get_client()
            from google.genai import types

            cfg = self._build_generation_config(req)
            cfg["system_instruction"] = "\n\n".join(b.text for b in req.system if b.text) or None
            cfg["tools"] = (
                [
                    types.Tool(
                        function_declarations=[
                            types.FunctionDeclaration(**d) for d in _translate_tools(req.tools)
                        ]
                    )
                ]
                if req.tools
                else None
            )
            cfg["tool_config"] = _translate_tool_choice(req.tool_choice) if req.tools else None
            # Linch owns execution and retry accounting, rather than SDK tool dispatch.
            cfg["automatic_function_calling"] = types.AutomaticFunctionCallingConfig(disable=True)
            config = types.GenerateContentConfig(**cfg)
            contents = [types.Content(**c) for c in _translate_messages(req.messages)]
        except LinchError:
            raise
        except Exception as exc:
            raise _map_error(exc) from exc

        yield {"type": "message_start", "model": req.model}
        counts = {
            "prompt_token_count": 0,
            "candidates_token_count": 0,
            "thoughts_token_count": 0,
            "cached_content_token_count": 0,
        }
        stop_reason: StopReason = "end_turn"
        seen_tool_calls: dict[str, dict[str, Any]] = {}
        model_parts: list[dict[str, Any]] = []
        try:
            raw_stream = await client.aio.models.generate_content_stream(
                model=req.model, contents=contents, config=config
            )
            async with aclosing(raw_stream):
                async for chunk in raw_stream:
                    if chunk.usage_metadata is not None:
                        for key in counts:
                            if (value := getattr(chunk.usage_metadata, key, None)) is not None:
                                counts[key] = value
                    if not chunk.candidates:
                        continue
                    candidate = chunk.candidates[0]
                    if candidate.finish_reason == types.FinishReason.MAX_TOKENS:
                        stop_reason = "max_tokens"
                    elif candidate.finish_reason in (
                        types.FinishReason.SAFETY,
                        types.FinishReason.RECITATION,
                    ):
                        stop_reason = "refusal"
                    elif candidate.finish_reason == types.FinishReason.MALFORMED_FUNCTION_CALL:
                        raise ProviderError(
                            "Gemini returned a malformed function call", retryable=True
                        )
                    for part in candidate.content.parts or [] if candidate.content else []:
                        fc = part.function_call
                        saved = part.model_dump(mode="json", exclude_none=True)
                        if fc is not None and fc.name:
                            tool_input = fc.args or {}
                            key = fc.id or json.dumps([fc.name, tool_input], sort_keys=True)
                            if key in seen_tool_calls:
                                if part.thought_signature:
                                    seen_tool_calls[key]["thought_signature"] = saved[
                                        "thought_signature"
                                    ]
                                continue
                            seen_tool_calls[key] = saved
                            tool_id = fc.id or f"gemini_{uuid.uuid4().hex[:8]}"
                            saved["function_call"]["id"] = tool_id
                            model_parts.append(saved)
                            yield {"type": "tool_use_start", "id": tool_id, "name": fc.name}
                            yield {
                                "type": "tool_use_input_delta",
                                "id": tool_id,
                                "json_delta": json.dumps(tool_input),
                            }
                            yield {"type": "tool_use_end", "id": tool_id}
                        else:
                            model_parts.append(saved)
                            if part.text:
                                yield {
                                    "type": "thinking_delta" if part.thought else "text_delta",
                                    "text": part.text,
                                }
        except LinchError:
            raise
        except Exception as exc:
            raise _map_error(exc) from exc
        yield {
            "type": "message_end",
            "stop_reason": "tool_use" if seen_tool_calls else stop_reason,
            "usage": Usage(
                input_tokens=counts["prompt_token_count"],
                output_tokens=counts["candidates_token_count"] + counts["thoughts_token_count"],
                cache_read_tokens=counts["cached_content_token_count"],
            ),
            "provider_metadata": {"gemini_parts": model_parts}
            if any("thought_signature" in p for p in model_parts)
            else None,
        }
