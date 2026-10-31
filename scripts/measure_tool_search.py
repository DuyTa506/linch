"""Measure the default tool-search prompt reduction and offering churn."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from linch import Agent, FunctionTool, InMemorySessionStore, ToolRegistry, ToolResult
from linch.loop.request import _build_turn_request
from linch.session import RunOptions


def _registry(count: int) -> ToolRegistry:
    """Build a deterministic catalog of read-only fake tools."""
    registry = ToolRegistry()
    for index in range(count):
        registry.register(
            FunctionTool(
                lambda value: value,
                name=f"lookup_{index}",
                description=f"Look up customer {index} records.",
                scope="read",
            )
        )
    return registry


class _HeartbeatTool:
    """Small pinned tool used to keep a single run moving between searches."""

    name = "heartbeat"
    description = "Record a no-op heartbeat for the measurement run."
    input_schema = {"type": "object", "properties": {}}
    scope = "read"
    parallel = False
    always_load = True

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Accept the empty heartbeat input."""
        return dict(raw)

    def summarize(self, input: dict[str, Any]) -> str:
        """Return the event summary for one heartbeat."""
        return "heartbeat()"

    async def execute(self, input: dict[str, Any], ctx: Any) -> ToolResult:
        """Return a compact successful heartbeat result."""
        return ToolResult(content="ok", summary="heartbeat()")


class _SearchTwiceProvider:
    """Emit two searches, pinned heartbeats, then a final text response."""

    id = "tool-search-measurement"

    def __init__(self) -> None:
        self.requests: list[Any] = []

    def context_window(self, model: str) -> int:
        return 128_000

    def capabilities(self, model: str) -> Any:
        from linch.providers.base import ProviderCapabilities

        return ProviderCapabilities(context_window=self.context_window(model))

    async def stream(self, request: Any) -> AsyncIterator[dict[str, Any]]:
        from linch.types import Usage

        self.requests.append(request)
        turn = len(self.requests)
        yield {"type": "message_start", "model": request.model}
        if turn in {1, 5}:
            query = f"select:lookup_{turn}"
            yield {"type": "tool_use_start", "id": f"search-{turn}", "name": "tool_search"}
            yield {
                "type": "tool_use_input_delta",
                "id": f"search-{turn}",
                "json_delta": json.dumps({"query": query}),
            }
            yield {"type": "tool_use_end", "id": f"search-{turn}"}
            yield {"type": "message_end", "stop_reason": "tool_use", "usage": Usage()}
            return
        if turn in {2, 3, 4, 6, 7, 8, 9}:
            yield {"type": "tool_use_start", "id": f"heartbeat-{turn}", "name": "heartbeat"}
            yield {
                "type": "tool_use_input_delta",
                "id": f"heartbeat-{turn}",
                "json_delta": json.dumps({"turn": turn}),
            }
            yield {"type": "tool_use_end", "id": f"heartbeat-{turn}"}
            yield {"type": "message_end", "stop_reason": "tool_use", "usage": Usage()}
            return
        if turn != 10:
            raise RuntimeError(f"unexpected provider turn {turn}")
        yield {"type": "text_delta", "text": "done"}
        yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}


async def _request_size(*, plugin: bool) -> int:
    """Return approximate serialized tool-list tokens for 60 tools."""
    provider = _SearchTwiceProvider()
    tools = _registry(59)
    tools.register(_HeartbeatTool())
    agent = Agent(
        model="measurement",
        provider=provider,
        tools=tools,
        session_store=InMemorySessionStore(),
        result_offload=None,
    )
    session = await agent.session()
    if not plugin:
        await agent.tool_search.dispose()
    request = _build_turn_request(session, RunOptions())
    chars = len(json.dumps(request.tools, sort_keys=True, separators=(",", ":")))
    await agent.close()
    return chars // 4


async def _run_ten_turns() -> tuple[int, int]:
    """Run one ten-request session and count serialized schema changes."""
    provider = _SearchTwiceProvider()
    tools = _registry(59)
    tools.register(_HeartbeatTool())
    agent = Agent(
        model="measurement",
        provider=provider,
        tools=tools,
        session_store=InMemorySessionStore(),
        result_offload=None,
    )
    session = await agent.session(id="measurement")
    async for _event in session.run("measure ten provider requests"):
        pass
    serialized = [
        json.dumps(request.tools, sort_keys=True, separators=(",", ":"))
        for request in provider.requests
    ]
    if len(serialized) != 10:
        raise RuntimeError(f"expected exactly 10 provider requests, got {len(serialized)}")
    changes = sum(
        current != previous for previous, current in zip(serialized, serialized[1:], strict=False)
    )
    await agent.close()
    return len(serialized), changes


async def main() -> None:
    """Print prompt-size and ten-turn offered-list measurements."""
    plain = await _request_size(plugin=False)
    deferred = await _request_size(plugin=True)
    requests, changes = await _run_ten_turns()
    print(f"60 tools serialized tokens (without plugin): {plain}")
    print(f"60 tools serialized tokens (with plugin):    {deferred}")
    print(f"10-turn provider requests recorded:          {requests}")
    print(f"offered tool-schema changes (incl. descriptions): {changes}")


if __name__ == "__main__":
    asyncio.run(main())
