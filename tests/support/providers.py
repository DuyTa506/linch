"""Small deterministic providers shared by unit tests."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from linch.providers import ProviderCapabilities
    from linch.types import ProviderRequest, Usage


@dataclass(slots=True)
class TextTurn:
    """Describe one scripted text response."""

    text: str
    stop_reason: str = "end_turn"
    usage: Usage | None = None

    def __post_init__(self) -> None:
        if self.usage is None:
            from linch.types import Usage

            self.usage = Usage()


@dataclass(slots=True)
class ToolUseTurn:
    """Describe one scripted tool-use response."""

    tool_name: str
    tool_input: dict[str, Any]
    tool_id: str = "scripted_tool_1"
    usage: Usage | None = None

    def __post_init__(self) -> None:
        if self.usage is None:
            from linch.types import Usage

            self.usage = Usage()


class ScriptedProvider:
    """Replay text and tool-use turns while recording provider requests.

    Args:
        turns: Responses to consume in order, one per provider call.
        context_window: Context-window size advertised by the provider.

    Raises:
        RuntimeError: If a call is made after all scripted turns are consumed.
    """

    id = "scripted-test"

    def __init__(
        self, turns: list[TextTurn | ToolUseTurn], *, context_window: int = 128_000
    ) -> None:
        self._turns = list(turns)
        self._index = 0
        self._context_window = context_window
        self.requests: list[ProviderRequest] = []

    def context_window(self, model: str) -> int:
        """Return the configured provider context window."""

        return self._context_window

    def capabilities(self, model: str) -> ProviderCapabilities:
        """Advertise the terminal-tool behavior used by scripted tests."""

        from linch.providers import ProviderCapabilities

        return ProviderCapabilities(
            context_window=self.context_window(model),
            structured_output_terminal_tool=True,
        )

    async def stream(self, req: ProviderRequest) -> AsyncIterator[dict[str, Any]]:
        """Yield normalized events for the next scripted response."""

        self.requests.append(req)
        if self._index >= len(self._turns):
            raise RuntimeError(
                f"ScriptedProvider ran out of turns (consumed {self._index}, "
                f"had {len(self._turns)})"
            )
        turn = self._turns[self._index]
        self._index += 1

        yield {"type": "message_start", "model": req.model}
        if isinstance(turn, TextTurn):
            yield {"type": "text_delta", "text": turn.text}
            yield {
                "type": "message_end",
                "stop_reason": turn.stop_reason,
                "usage": turn.usage,
                "provider_metadata": None,
            }
            return

        yield {"type": "tool_use_start", "id": turn.tool_id, "name": turn.tool_name}
        yield {
            "type": "tool_use_input_delta",
            "id": turn.tool_id,
            "json_delta": json.dumps(turn.tool_input),
        }
        yield {"type": "tool_use_end", "id": turn.tool_id}
        yield {
            "type": "message_end",
            "stop_reason": "tool_use",
            "usage": turn.usage,
            "provider_metadata": None,
        }
