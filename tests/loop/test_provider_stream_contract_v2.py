"""Strict normalized provider-stream contract regressions."""

from __future__ import annotations

from typing import Any

import pytest


class _Provider:
    id = "strict-stream"

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events

    def context_window(self, model: str) -> int:
        return 128_000

    async def stream(self, req: Any):
        for event in self.events:
            yield event


async def _run(events: list[dict[str, Any]]) -> list[Any]:
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools

    agent = Agent(
        model="test-model",
        provider=_Provider(events),
        tools=empty_tools(),
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        features=FeatureFlags(),
        result_offload=None,
    )
    session = await agent.session()
    return [event async for event in session.run("go")]


def _start() -> dict[str, Any]:
    return {"type": "message_start", "model": "test-model"}


def _end(stop_reason: str = "tool_use") -> dict[str, Any]:
    from linch.types import Usage

    return {"type": "message_end", "stop_reason": stop_reason, "usage": Usage()}


@pytest.mark.parametrize(
    "events,detail",
    [
        ([], "message_start"),
        ([_start()], "message_end"),
        ([{"type": "text_delta", "text": "x"}, _end("end_turn")], "first event"),
        ([_start(), _start(), _end("end_turn")], "duplicate message_start"),
        ([_start(), _end("tool_use")], "at least one complete tool call"),
        (
            [
                _start(),
                {"type": "message_end", "stop_reason": [], "usage": _end()["usage"]},
            ],
            "invalid message_end.stop_reason",
        ),
        (
            [_start(), {"type": "tool_use_input_delta", "id": "x", "json_delta": "{}"}],
            "unknown or closed",
        ),
        ([_start(), {"type": "tool_use_end", "id": "x"}], "unknown or closed"),
        (
            [
                _start(),
                {"type": "tool_use_start", "id": "x", "name": "A"},
                {"type": "tool_use_start", "id": "x", "name": "B"},
            ],
            "duplicate tool-use id",
        ),
        (
            [
                _start(),
                {"type": "tool_use_start", "id": "x", "name": "A"},
                _end("tool_use"),
            ],
            "before tool_use_end",
        ),
    ],
)
async def test_malformed_or_truncated_stream_fails_deterministically(
    events: list[dict[str, Any]], detail: str
) -> None:
    out = await _run(events)
    errors = [event for event in out if event.type == "error"]
    assert len(errors) == 1
    assert errors[0].error["name"] == "ProviderError"
    assert detail in errors[0].error["message"]
    assert out[-1].type == "result" and out[-1].subtype == "error"
