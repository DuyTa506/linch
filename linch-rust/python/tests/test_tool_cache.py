"""`spec.tool_cache`: a repeated call of a named read tool is served from the cache."""

from __future__ import annotations

from typing import Any

from scripted import MODEL, config, text_turn, tool_turn

from linch_rust import AgentConfig, Server, ToolCache, ToolCallEndEvent, tool


def caching(turns: list[dict[str, Any]], cache: ToolCache) -> AgentConfig:
    return config(turns, spec={"model": MODEL, "tool_cache": cache})


async def test_a_repeated_call_is_served_from_the_cache() -> None:
    ran: list[str] = []

    @tool(scope="read")
    async def lookup(q: str) -> str:
        ran.append(q)
        return "found"

    turns = [
        tool_turn("call_1", "lookup", {"q": "x"}),
        tool_turn("call_2", "lookup", {"q": "x"}),
        tool_turn("call_3", "lookup", {"q": "y"}),
        text_turn("Done."),
    ]
    async with Server() as server:
        agent = await server.open_agent(caching(turns, {"tools": ["lookup"]}), tools=[lookup])
        run = await agent.run("s1", "go")
        envelopes = [e async for e in run]

    assert ran == ["x", "y"]
    ends = [e.event for e in envelopes if isinstance(e.event, ToolCallEndEvent)]
    assert [(end.tool_use_id, end.cache_hit) for end in ends] == [
        ("call_1", False),
        ("call_2", True),
        ("call_3", False),
    ]


async def test_a_tool_the_cache_does_not_name_always_runs() -> None:
    ran: list[str] = []

    @tool(scope="read")
    async def lookup(q: str) -> str:
        ran.append(q)
        return "found"

    turns = [
        tool_turn("call_1", "lookup", {"q": "x"}),
        tool_turn("call_2", "lookup", {"q": "x"}),
        text_turn("Done."),
    ]
    async with Server() as server:
        agent = await server.open_agent(caching(turns, {"tools": ["other"]}), tools=[lookup])
        run = await agent.run("s1", "go")
        envelopes = [e async for e in run]

    assert ran == ["x", "x"]
    ends = [e.event for e in envelopes if isinstance(e.event, ToolCallEndEvent)]
    assert not any(end.cache_hit for end in ends)
