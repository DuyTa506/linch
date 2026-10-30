from __future__ import annotations

import json
from typing import Any

import pytest

from examples.memory.mem0_recall import Mem0ContextBuilder, Mem0RecallHook
from linch import ContextBuildTurn, Message, RunOptions, TextBlock
from linch.providers.base import BaseProvider
from linch.types import ToolResultBlock, ToolUseBlock, Usage


class FakeAsyncMemory:
    def __init__(self, hits: Any = None) -> None:
        self.hits = hits if hits is not None else {"results": []}
        self.search_calls: list[tuple[str, dict[str, str]]] = []
        self.add_calls: list[tuple[list[dict[str, str]], str]] = []

    async def search(self, query: str, *, filters: dict[str, str]):
        self.search_calls.append((query, filters))
        return self.hits

    async def add(self, messages: list[dict[str, str]], *, user_id: str):
        self.add_calls.append((messages, user_id))


def _turn(client: FakeAsyncMemory, user_id: str, text: str) -> ContextBuildTurn:
    return ContextBuildTurn(
        session=None,  # type: ignore[arg-type]
        messages=[Message(role="user", content=[TextBlock(text=text)])],
        turn_index=0,
        deps={"mem0": client, "user_id": user_id},
        model="gpt-5",
        tools=None,
    )


@pytest.mark.asyncio
async def test_mem0_builder_searches_latest_user_text_with_scoped_filter() -> None:
    client = FakeAsyncMemory({"results": [{"id": "m1", "memory": "User prefers concise answers."}]})

    result = await Mem0ContextBuilder().build(_turn(client, "user-a", "How short?"))

    assert client.search_calls == [("How short?", {"user_id": "user-a"})]
    assert result.messages
    assert "User prefers concise answers." in result.messages[0].content[0].text


@pytest.mark.asyncio
async def test_mem0_builder_keeps_concurrent_users_scoped() -> None:
    client = FakeAsyncMemory()

    await Mem0ContextBuilder().build(_turn(client, "user-a", "A"))
    await Mem0ContextBuilder().build(_turn(client, "user-b", "B"))

    assert client.search_calls == [
        ("A", {"user_id": "user-a"}),
        ("B", {"user_id": "user-b"}),
    ]


@pytest.mark.asyncio
async def test_mem0_builder_ignores_tool_result_user_messages_for_query() -> None:
    client = FakeAsyncMemory()
    turn = ContextBuildTurn(
        session=None,  # type: ignore[arg-type]
        messages=[
            Message(role="user", content=[TextBlock(text="Remember this")]),
            Message(
                role="assistant",
                content=[ToolUseBlock(id="tool-1", name="Lookup", input={})],
            ),
            Message(
                role="user",
                content=[ToolResultBlock(tool_use_id="tool-1", content="tool output")],
            ),
        ],
        turn_index=1,
        deps={"mem0": client, "user_id": "user-a"},
        model="gpt-5",
        tools=None,
    )

    await Mem0ContextBuilder().build(turn)

    assert client.search_calls == [("Remember this", {"user_id": "user-a"})]


@pytest.mark.asyncio
async def test_mem0_hook_adds_only_after_successful_agent_run_with_tool_turn() -> None:
    class RecordingTool:
        name = "Remember"
        description = "Record a note."
        input_schema = {"type": "object", "properties": {}}
        scope = "read"
        parallel = True

        def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
            return raw

        async def execute(self, input: dict[str, Any], ctx: Any):
            from linch import ToolResult

            return ToolResult(content="recorded")

        def summarize(self, input: dict[str, Any]) -> str:
            return self.name

    class ToolThenFinalProvider(BaseProvider):
        id = "fake"

        def __init__(self) -> None:
            self.calls = 0

        def context_window(self, model: str) -> int:
            return 128_000

        async def stream(self, req):
            self.calls += 1
            yield {"type": "message_start", "model": req.model}
            if self.calls == 1:
                yield {"type": "tool_use_start", "id": "tool-1", "name": "Remember"}
                yield {
                    "type": "tool_use_input_delta",
                    "id": "tool-1",
                    "json_delta": json.dumps({}),
                }
                yield {"type": "tool_use_end", "id": "tool-1"}
                yield {"type": "message_end", "stop_reason": "tool_use", "usage": Usage()}
                return
            yield {"type": "text_delta", "text": "done"}
            yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}

    client = FakeAsyncMemory()
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools

    provider = ToolThenFinalProvider()
    agent = Agent(
        model="gpt-5",
        provider=provider,
        tools=empty_tools(RecordingTool()),
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        hooks=[Mem0RecallHook()],
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        result_offload=None,
    )
    session = await agent.session()
    events = [
        event
        async for event in session.run(
            "Remember this", RunOptions(deps={"mem0": client, "user_id": "user-a"})
        )
    ]

    assert provider.calls == 2
    assert any(event.type == "tool_call_end" for event in events)
    assert client.add_calls == [
        (
            [
                {"role": "user", "content": "Remember this"},
                {"role": "assistant", "content": "done"},
            ],
            "user-a",
        )
    ]


@pytest.mark.asyncio
async def test_mem0_hook_skips_non_successful_agent_run() -> None:
    class FailingProvider(BaseProvider):
        id = "fake"

        def context_window(self, model: str) -> int:
            return 128_000

        async def stream(self, req):
            raise RuntimeError("provider failed")
            yield  # pragma: no cover

    client = FakeAsyncMemory()
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.sessions import InMemorySessionStore

    agent = Agent(
        model="gpt-5",
        provider=FailingProvider(),
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        hooks=[Mem0RecallHook()],
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        result_offload=None,
    )
    session = await agent.session()
    async for _event in session.run(
        "Remember this", RunOptions(deps={"mem0": client, "user_id": "user-a"})
    ):
        pass

    assert client.add_calls == []
