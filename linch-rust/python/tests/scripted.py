"""Scripted-provider turns and agent configs for the tests."""

from __future__ import annotations

import json
from typing import Any

MODEL = "scripted-model"
_USAGE = {"input_tokens": 3, "output_tokens": 1, "cache_read_tokens": 0, "cache_creation_tokens": 0}


def text_turn(text: str) -> dict[str, Any]:
    """A provider turn that answers `text` and ends the run."""
    return {
        "events": [
            {"type": "message_start", "model": MODEL},
            {"type": "text_delta", "text": text},
            {"type": "message_end", "stop_reason": "end_turn", "usage": _USAGE},
        ]
    }


def tool_turn(call_id: str, name: str, input: dict[str, Any]) -> dict[str, Any]:
    """A provider turn that calls tool `name` once."""
    return {
        "events": [
            {"type": "message_start", "model": MODEL},
            {"type": "tool_use_start", "id": call_id, "name": name},
            {"type": "tool_use_input_delta", "id": call_id, "json_delta": json.dumps(input)},
            {"type": "tool_use_end", "id": call_id},
            {"type": "message_end", "stop_reason": "tool_use", "usage": _USAGE},
        ]
    }


def config(turns: list[dict[str, Any]], mode: str = "default", **extra: Any) -> dict[str, Any]:
    """A scripted agent config with no workspace, in permission mode `mode`."""
    return {
        "namespace": "p12",
        "spec": {"model": MODEL},
        "provider": {"id": "scripted", "turns": turns},
        "permissions": {"mode": mode, "project_root": "/"},
        **extra,
    }
