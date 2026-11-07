"""Intercept callbacks answered in this process."""

from __future__ import annotations

from typing import Any

import pytest
from scripted import config, text_turn, tool_turn

from linch_rust import (
    ContextBuildEvent,
    HookEvent,
    PromptBlocked,
    ResultEvent,
    Server,
    ToolCallEndEvent,
    callback,
    tool,
)


async def test_a_pre_tool_use_callback_blocks_a_call() -> None:
    ran: list[str] = []
    asked: list[dict[str, Any]] = []

    @tool(scope="read")
    async def lookup(q: str) -> str:
        ran.append(q)
        return "found"

    @callback("pre_tool_use", "no-lookups", 1, matcher=["look*"])
    async def guard(request: dict[str, Any]) -> dict[str, Any]:
        asked.append(request)
        return {"action": "block", "reason": "lookups are off today"}

    turns = [tool_turn("call_1", "lookup", {"q": "x"}), text_turn("Blocked, then.")]
    async with Server() as server:
        agent = await server.open_agent(config(turns), tools=[lookup], callbacks=[guard])
        run = await agent.run("s1", "Look up x")
        envelopes = [e async for e in run]

    assert ran == []
    [request] = asked
    assert request["tool_name"] == "lookup" and request["input"] == {"q": "x"}
    assert request["run_id"] == run.id
    [end] = [e.event for e in envelopes if isinstance(e.event, ToolCallEndEvent)]
    assert end.is_error and "lookups are off today" in str(end.result)
    hooks = [e.event for e in envelopes if isinstance(e.event, HookEvent)]
    assert [(h.point, h.policy_id, h.outcome["status"]) for h in hooks] == [
        ("pre_tool_use", "no-lookups", "answered")
    ]
    result = envelopes[-1].event
    assert isinstance(result, ResultEvent) and result.subtype == "success"


async def test_context_build_metadata_lands_on_the_event_not_the_request() -> None:
    @callback("context_build", "rag", 1)
    async def rag(request: dict[str, Any]) -> dict[str, Any]:
        return {"blocks": [{"text": "doc 7"}], "metadata": {"rag": "p1"}}

    async with Server() as server:
        agent = await server.open_agent(config([text_turn("ok")]), callbacks=[rag])
        run = await agent.run("s1", "go")
        envelopes = [e async for e in run]

    [build] = [e.event for e in envelopes if isinstance(e.event, ContextBuildEvent)]
    assert build.metadata == {"rag": "p1"}


async def test_a_user_prompt_submit_block_raises_prompt_blocked() -> None:
    @callback("user_prompt_submit", "no-secrets", 1)
    async def screen(request: dict[str, Any]) -> dict[str, Any]:
        return {"action": "block", "reason": "mentions a secret"}

    async with Server() as server:
        agent = await server.open_agent(config([text_turn("unused")]), callbacks=[screen])
        run = await agent.run("s1", "the password is hunter2")
        with pytest.raises(PromptBlocked) as raised:
            await run.result()

    assert raised.value.reason == "mentions a secret"
