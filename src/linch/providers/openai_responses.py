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
from linch.providers.base import BaseProvider, ProviderCapabilities, full_capabilities
from linch.types import ModelId, ProviderRequest


@dataclass(slots=True)
class OpenAIResponsesProviderOptions:
    """Configure the OpenAI Responses adapter.

    Args:
        api_key: Optional client credential.
        base_url: Optional Responses-compatible endpoint override.
        default_headers: Headers added to client requests.
        reasoning: Optional shared reasoning configuration.

    Attributes:
        reasoning: Shared reasoning settings forwarded to the Responses client.
    """

    api_key: str | None = None
    base_url: str | None = None
    default_headers: dict[str, str] | None = None
    reasoning: OpenAIReasoning | None = None


class OpenAIResponsesProvider(BaseProvider):
    """Stream normalized events from OpenAI's Responses API.

    Args:
        options: Optional client credentials, endpoint, and reasoning settings.
    """

    id = "openai-responses"

    def __init__(self, options: OpenAIResponsesProviderOptions | None = None) -> None:
        """Create an OpenAI Responses provider.

        Args:
            options: Client credentials, endpoint, and reasoning settings.
        """
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
        """Return the known OpenAI Responses context capacity.

        Args:
            model: OpenAI Responses model identifier.

        Returns:
            The model's context capacity as known by the Responses adapter.
        """
        return context_window(model)

    def capabilities(self, model: ModelId) -> ProviderCapabilities:
        """Return capabilities advertised for a Responses model.

        Args:
            model: OpenAI Responses model identifier.

        Returns:
            Schema, cache, tool-choice, and parallel-call capabilities.
        """
        return full_capabilities(self.context_window(model))

    async def stream(self, req: ProviderRequest) -> AsyncIterator[dict[str, object]]:
        """Yield normalized events for one Responses request.

        Args:
            req: Request to send through the Responses client.

        Returns:
            An async iterator of normalized provider events.
        """
        wire = self._client.stream(req)
        async for event in map_wire_events(wire, req.model):
            yield event

    async def aclose(self) -> None:
        """Close the owned Responses client and its transport."""
        await self._client.aclose()
