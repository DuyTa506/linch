from __future__ import annotations

from typing import Any

import pytest

from examples.memory.mem0_recall import Mem0ContextBuilder, Mem0RecallHook
from linch import ContextBuildTurn, Message, RunOptions, TextBlock
from linch.config import FeatureFlags
from linch.providers.base import BaseProvider
from linch.types import ToolResultBlock, ToolUseBlock
from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn, ToolUseTurn
from tests.support.tools import StubTool, tool_registry


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
    assert "User prefers concise answers." in getattr(result.messages[0].content[0], "text", "")


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
    client = FakeAsyncMemory()
    provider = ScriptedProvider([ToolUseTurn("Remember", {}, tool_id="tool-1"), TextTurn("done")])
    agent = make_agent(
        provider,
        model="gpt-5",
        tools=tool_registry(StubTool("Remember", output="recorded", parallel=True)),
        permissions={"mode": "skip-dangerous"},
        run_store=None,
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

    assert len(provider.requests) == 2
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
    agent = make_agent(
        FailingProvider(),
        model="gpt-5",
        run_store=None,
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
