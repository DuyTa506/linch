from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest


class RecordingProvider:
    id = "fake"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def context_window(self, model: str) -> int:
        return 128_000

    async def stream(self, req):
        from linch.types import TextBlock, Usage

        self.calls.append(
            {
                "system": [block.text for block in req.system],
                "messages": [
                    {
                        "role": message.role,
                        "content": [
                            block.text if isinstance(block, TextBlock) else str(block)
                            for block in message.content
                        ],
                    }
                    for message in req.messages
                ],
                "tools": [tool["name"] for tool in req.tools],
            }
        )
        yield {"type": "message_start", "model": req.model}
        yield {"type": "text_delta", "text": "done"}
        yield {
            "type": "message_end",
            "stop_reason": "end_turn",
            "usage": Usage(),
            "provider_metadata": None,
        }


def _fake_tool(name: str, tags: tuple[str, ...] = ()):
    class FakeTool:
        description = f"Fake {name}"
        input_schema = {"type": "object", "properties": {}}
        scope = "read"
        parallel = True

        def __init__(self) -> None:
            self.name = name
            self.tags = tags

        def validate(self, raw):
            return raw

        async def execute(self, input, ctx):
            from linch.tools import ToolResult

            return ToolResult(content="ok", summary=self.name)

        def summarize(self, input):
            return self.name

    return FakeTool()


def _agent(provider: Any, *, context_builder: Any):
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools

    return Agent(
        model="gpt-5",
        provider=provider,
        tools=empty_tools(_fake_tool("SearchDocs", ("rag",)), _fake_tool("WriteNote")),
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        context_builder=context_builder,
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        result_offload=None,
    )


async def _drain(session, prompt: str = "hello"):
    events = []
    async for event in session.run(prompt):
        events.append(event)
    return events


class StaticBuilder:
    async def build(self, turn):
        from linch import ContextBuildResult
        from linch.types import Message, SystemBlock, TextBlock

        return ContextBuildResult(
            system_blocks=[SystemBlock(text=f"CTX_SYSTEM turn={turn.turn_index}")],
            messages=[
                Message(
                    role="user",
                    content=[TextBlock(text=f"CTX_MESSAGE turn={turn.turn_index}")],
                )
            ],
            metadata={"source": "static"},
        )


@pytest.mark.asyncio
async def test_context_builder_reaches_provider_without_persisting():
    provider = RecordingProvider()
    agent = _agent(provider, context_builder=StaticBuilder())
    session = await agent.session()

    events = await _drain(session)

    assert any(event.type == "context_build" for event in events)
    assert any("CTX_SYSTEM turn=0" in text for text in provider.calls[0]["system"])
    all_text = [text for msg in provider.calls[0]["messages"] for text in msg["content"]]
    assert "CTX_MESSAGE turn=0" in all_text

    persisted = [text for msg in session.provider_view for text in getattr(msg, "content", [])]
    assert all(getattr(block, "text", "") != "CTX_MESSAGE turn=0" for block in persisted)


@pytest.mark.asyncio
async def test_context_builders_run_in_stable_order():
    from linch import ContextBuildResult
    from linch.types import Message, TextBlock

    class Builder:
        def __init__(self, label: str) -> None:
            self.label = label

        async def build(self, turn):
            return ContextBuildResult(
                messages=[Message(role="user", content=[TextBlock(text=f"context-{self.label}")])],
                metadata={self.label: turn.turn_index},
            )

    provider = RecordingProvider()
    agent = _agent(provider, context_builder=[Builder("a"), Builder("b")])
    session = await agent.session()
    events = await _drain(session)

    texts = [text for msg in provider.calls[0]["messages"] for text in msg["content"]]
    assert texts.index("context-a") < texts.index("context-b")
    context_event = next(event for event in events if event.type == "context_build")
    assert context_event.metadata == {"a": 0, "b": 0}


@pytest.mark.asyncio
async def test_context_budget_trims_ephemeral_messages():
    from linch import ContextBudget, ContextBuildResult
    from linch.types import Message, TextBlock

    class BudgetBuilder:
        async def build(self, turn):
            return ContextBuildResult(
                messages=[
                    Message(role="user", content=[TextBlock(text="first context block")]),
                    Message(role="user", content=[TextBlock(text="second")]),
                ],
                budget=ContextBudget(max_tokens=2),
            )

    provider = RecordingProvider()
    agent = _agent(provider, context_builder=BudgetBuilder())
    session = await agent.session()
    events = await _drain(session)

    texts = [text for msg in provider.calls[0]["messages"] for text in msg["content"]]
    assert "first context block" not in texts
    assert "second" in texts
    context_event = next(event for event in events if event.type == "context_build")
    assert context_event.budget["trimmed"] is True
    assert context_event.budget["used_tokens"] <= 2


@pytest.mark.asyncio
async def test_context_selected_tools_are_request_scoped():
    from linch import ContextBuildResult

    class SelectingBuilder:
        async def build(self, turn):
            return ContextBuildResult(selected_tools={"SearchDocs"})

    provider = RecordingProvider()
    agent = _agent(provider, context_builder=SelectingBuilder())
    session = await agent.session()
    await _drain(session)

    assert provider.calls[0]["tools"] == ["SearchDocs"]
    assert sorted(tool.name for tool in agent.tools.list()) == ["SearchDocs", "WriteNote"]


@pytest.mark.asyncio
async def test_context_builder_reruns_after_compaction_retry():
    from linch import ContextBuildResult, ContextLengthError
    from linch.types import Message, TextBlock

    class RetryProvider(RecordingProvider):
        async def stream(self, req):
            if not self.calls:
                self.calls.append({"first": True})
                raise ContextLengthError("too long")
            async for event in super().stream(req):
                yield event

    @dataclass
    class NoopCompaction:
        id: str = "noop"

        async def compact(self, ctx, provider):
            return list(ctx.messages)

    class CountingBuilder:
        def __init__(self) -> None:
            self.calls = 0

        async def build(self, turn):
            self.calls += 1
            return ContextBuildResult(
                messages=[
                    Message(
                        role="user",
                        content=[TextBlock(text=f"fresh-context-{self.calls}")],
                    )
                ]
            )

    builder = CountingBuilder()
    provider = RetryProvider()
    agent = _agent(provider, context_builder=builder)
    agent.compaction = NoopCompaction()
    session = await agent.session()
    events = await _drain(session)

    assert builder.calls == 2
    assert [event.type for event in events].count("context_build") == 2
    texts = [text for msg in provider.calls[-1]["messages"] for text in msg["content"]]
    assert "fresh-context-2" in texts
