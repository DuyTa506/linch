from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

from agent_kit.errors import ProviderError
from agent_kit.providers.base import BaseProvider
from agent_kit.types import ModelId, ProviderRequest

_KNOWN_CONTEXT = {
    "claude-opus-4-8": 200_000,
    "claude-sonnet-4-6": 200_000,
    "claude-haiku-4-5": 200_000,
}


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

    def context_window(self, model: ModelId) -> int:
        return _KNOWN_CONTEXT.get(model, 200_000)

    async def stream(self, req: ProviderRequest) -> AsyncIterator[dict[str, object]]:
        raise ProviderError(
            "AnthropicProvider is available in the public surface but streaming is "
            "not configured in this build yet. Use OpenAI providers for now."
        )
        yield {"type": "message_start", "model": req.model}
