"""Turns for the `scripted` provider, which replays them in call order.

They run an agent offline, in tests or before a provider key exists. Each
call to the model takes the next turn, a subagent's calls included::

    config: AgentConfig = {
        "namespace": "demo",
        "spec": {"model": MODEL},
        "provider": {"id": "scripted", "turns": [text_turn("Hello.")]},
    }
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

MODEL = "scripted-model"
_USAGE = {"input_tokens": 3, "output_tokens": 1, "cache_read_tokens": 0, "cache_creation_tokens": 0}


def text_turn(text: str, model: str = MODEL) -> dict[str, Any]:
    """A turn from `model` that answers `text`; a run ends on it."""
    return {
        "events": [
            {"type": "message_start", "model": model},
            {"type": "text_delta", "text": text},
            {"type": "message_end", "stop_reason": "end_turn", "usage": _USAGE},
        ]
    }


def tool_turn(
    call_id: str, name: str, input: Mapping[str, Any], model: str = MODEL
) -> dict[str, Any]:
    """A turn from `model` that calls tool `name` once with `input`, as call `call_id`."""
    return {
        "events": [
            {"type": "message_start", "model": model},
            {"type": "tool_use_start", "id": call_id, "name": name},
            {"type": "tool_use_input_delta", "id": call_id, "json_delta": json.dumps(input)},
            {"type": "tool_use_end", "id": call_id},
            {"type": "message_end", "stop_reason": "tool_use", "usage": _USAGE},
        ]
    }
