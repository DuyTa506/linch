"""ScriptedProvider — canonical scripted fake provider for evals and tests.

Replaces the copy-pasted fake providers scattered across tests/providers/,
tests/loop/, etc. Each turn is declared as a TextTurn or ToolUseTurn and
consumed in sequence; the provider errors if turns run out.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from ..providers.base import BaseProvider
from ..types import Usage


@dataclass
class TextTurn:
    """Provider turn that yields a single text response."""

    text: str
    stop_reason: str = "end_turn"
    usage: Usage = field(default_factory=Usage)


@dataclass
class ToolUseTurn:
    """Provider turn that emits one tool-use block."""

    tool_name: str
    tool_input: dict[str, Any]
    tool_id: str = "scripted_tool_1"
    usage: Usage = field(default_factory=Usage)


Turn = TextTurn | ToolUseTurn


class ScriptedProvider(BaseProvider):
    """A provider whose responses are fully scripted via a list of turns.

    Each call to ``stream()`` consumes the next turn in the sequence.
    Useful for deterministic unit tests and eval cases — no network required.

    Example::

        provider = ScriptedProvider(turns=[
            ToolUseTurn(tool_name="Read", tool_input={"file_path": "README.md"}),
            TextTurn(text="Here is the summary: ..."),
        ])
    """

    id = "scripted"

    def __init__(self, turns: list[Turn]) -> None:
        self._turns = list(turns)
        self._index = 0

    def context_window(self, model: str) -> int:
        return 128_000

    async def stream(self, req) -> AsyncIterator[dict[str, Any]]:
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
        elif isinstance(turn, ToolUseTurn):
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
