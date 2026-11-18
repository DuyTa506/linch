"""`spec.tool_retry`: an idempotent host tool raising `ToolError` with a listed class runs again."""

from __future__ import annotations

from typing import Any

from scripted import MODEL, config, text_turn, tool_turn

from linch_rust import (
    AgentConfig,
    Server,
    ToolCallEndEvent,
    ToolError,
    ToolRetry,
    ToolRetryEvent,
    tool,
)

POLICY: ToolRetry = {"base_delay_ms": 1, "max_delay_ms": 2}


def retrying(turns: list[dict[str, Any]]) -> AgentConfig:
    return config(turns, spec={"model": MODEL, "tool_retry": POLICY})


async def test_an_idempotent_tool_that_raises_a_transient_error_is_retried() -> None:
    attempts: list[str] = []

    @tool(scope="read", idempotent=True)
    async def lookup(q: str) -> str:
        attempts.append(q)
        if len(attempts) < 3:
            raise ToolError("busy", code="transient")
        return "found"

    turns = [tool_turn("call_1", "lookup", {"q": "x"}), text_turn("Done.")]
    async with Server() as server:
        agent = await server.open_agent(retrying(turns), tools=[lookup])
        run = await agent.run("s1", "go")
        envelopes = [e async for e in run]

    assert attempts == ["x", "x", "x"]
    retries = [e.event for e in envelopes if isinstance(e.event, ToolRetryEvent)]
    assert [(r.attempt, r.max_attempts, r.error_class) for r in retries] == [
        (2, 3, "transient"),
        (3, 3, "transient"),
    ]
    [end] = [e.event for e in envelopes if isinstance(e.event, ToolCallEndEvent)]
    assert not end.is_error and not end.cache_hit


async def test_a_tool_that_is_not_idempotent_runs_once() -> None:
    attempts: list[str] = []

    @tool(scope="read")
    async def lookup(q: str) -> str:
        attempts.append(q)
        raise ToolError("busy", code="transient")

    turns = [tool_turn("call_1", "lookup", {"q": "x"}), text_turn("Done.")]
    async with Server() as server:
        agent = await server.open_agent(retrying(turns), tools=[lookup])
        run = await agent.run("s1", "go")
        envelopes = [e async for e in run]

    assert attempts == ["x"]
    assert not [e for e in envelopes if isinstance(e.event, ToolRetryEvent)]
    [end] = [e.event for e in envelopes if isinstance(e.event, ToolCallEndEvent)]
    assert end.is_error and end.error_code == "transient"
