from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

from linch.openai_responses import (
    OpenAIOptions,
    OpenAIReasoning,
    OpenAIResponsesClient,
    context_window,
    map_wire_events,
)
from linch.providers.base import BaseProvider, ProviderCapabilities
from linch.types import ModelId, ProviderRequest


@dataclass(slots=True)
class OpenAIResponsesProviderOptions:
    api_key: str | None = None
    base_url: str | None = None
    default_headers: dict[str, str] | None = None
    reasoning: OpenAIReasoning | None = None


class OpenAIResponsesProvider(BaseProvider):
    id = "openai-responses"

    def __init__(self, options: OpenAIResponsesProviderOptions | None = None) -> None:
        opts = options or OpenAIResponsesProviderOptions()
        self._client = OpenAIResponsesClient(
            OpenAIOptions(
                api_key=opts.api_key,
                base_url=opts.base_url,
                default_headers=opts.default_headers,
            ),
            opts.reasoning,
        )

    def context_window(self, model: ModelId) -> int:
        return context_window(model)

    def capabilities(self, model: ModelId) -> ProviderCapabilities:
        return ProviderCapabilities(
            context_window=self.context_window(model),
            parallel_tool_calls=True,
            structured_output=True,
            tool_choice=True,
            prompt_cache=True,
        )

    async def stream(self, req: ProviderRequest) -> AsyncIterator[dict[str, object]]:
        wire = self._client.stream(req)
        async for event in map_wire_events(wire, req.model):
            yield event
