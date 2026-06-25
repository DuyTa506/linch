"""Provider extension template.

Copy this file when adding a provider adapter. Replace ``TemplateProvider``'s
``stream`` method with your vendor wire call, but keep the normalized event
shape Linch expects.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

from linch.providers import BaseProvider, ProviderCapabilities
from linch.types import ProviderRequest, TextBlock, Usage


class TemplateProvider(BaseProvider):
    """Minimal provider adapter with normalized streaming events."""

    id = "template"

    def __init__(self, responder: Callable[[ProviderRequest], str] | None = None) -> None:
        self._responder = responder or _default_response

    def context_window(self, model: str) -> int:
        return 128_000

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(
            context_window=self.context_window(model),
            parallel_tool_calls=True,
            structured_output=False,
            tool_choice=True,
            prompt_cache=False,
        )

    async def stream(self, req: ProviderRequest) -> AsyncIterator[dict[str, Any]]:
        text = self._responder(req)

        yield {"type": "message_start", "model": req.model}
        yield {"type": "text_delta", "text": text}
        yield {
            "type": "message_end",
            "stop_reason": "end_turn",
            "usage": Usage(input_tokens=_estimate_request_tokens(req), output_tokens=len(text)),
            "provider_metadata": {"provider": self.id},
        }


def _default_response(req: ProviderRequest) -> str:
    for message in reversed(req.messages):
        if message.role != "user":
            continue
        parts = [block.text for block in message.content if isinstance(block, TextBlock)]
        if parts:
            return "Echo: " + " ".join(parts)
    return "Echo:"


def _estimate_request_tokens(req: ProviderRequest) -> int:
    total = 0
    for block in req.system:
        total += len(block.text)
    for message in req.messages:
        for block in message.content:
            total += len(getattr(block, "text", ""))
    return total
