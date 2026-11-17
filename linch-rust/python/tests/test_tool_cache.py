"""`spec.tool_cache`: a repeated call of a named read tool is served from the cache."""

from __future__ import annotations

from typing import Any, Literal

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


async def _second_session_hits(scope: Literal["session", "namespace"]) -> bool:
    @tool(scope="read")
    async def lookup(q: str) -> str:
        return "found"

    turns = [
        tool_turn("call_1", "lookup", {"q": "x"}),
        text_turn("One."),
        tool_turn("call_2", "lookup", {"q": "x"}),
        text_turn("Two."),
    ]
    cache: ToolCache = {"tools": ["lookup"], "scope": scope}
    hits: list[bool] = []
    async with Server() as server:
        agent = await server.open_agent(caching(turns, cache), tools=[lookup])
        for session in ("s1", "s2"):
            run = await agent.run(session, "go")
            envelopes = [e async for e in run]
            [end] = [e.event for e in envelopes if isinstance(e.event, ToolCallEndEvent)]
            hits.append(end.cache_hit)
    assert hits[0] is False
    return hits[1]


async def test_another_session_is_served_only_when_the_scope_is_the_namespace() -> None:
    assert await _second_session_hits("session") is False
    assert await _second_session_hits("namespace") is True


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
