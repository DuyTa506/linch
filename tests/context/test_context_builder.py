from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import pytest

from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn
from tests.support.tools import StubTool, tool_registry


def _fake_tool(name: str, tags: tuple[str, ...] = ()) -> StubTool:
    tool = StubTool(name, parallel=True)
    cast(Any, tool).tags = tags
    return tool


def _context_hook(builder: Any):
    from linch.hooks import ContextInjectionHook

    return ContextInjectionHook(builder)


def _agent(provider: Any, *, hooks: Any):
    from linch.config import FeatureFlags

    return make_agent(
        provider,
        model="gpt-5",
        tools=tool_registry(_fake_tool("SearchDocs", ("rag",)), _fake_tool("WriteNote")),
        hooks=hooks,
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        result_offload=None,
        run_store=None,
    )


async def _drain(session, prompt: str = "hello"):
    return [event async for event in session.run(prompt)]


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

    provider = ScriptedProvider([TextTurn("done")])
    agent = _agent(provider, hooks=[_context_hook([StaticBuilder(), Builder("a"), Builder("b")])])
    session = await agent.session()
    events = await _drain(session)

    request = provider.requests[0]
    assert any("CTX_SYSTEM turn=0" in getattr(block, "text", "") for block in request.system)
    all_text = [getattr(block, "text", "") for msg in request.messages for block in msg.content]
    assert "CTX_MESSAGE turn=0" in all_text
    persisted = [block for msg in session.provider_view for block in getattr(msg, "content", [])]
    assert all(getattr(block, "text", "") != "CTX_MESSAGE turn=0" for block in persisted)

    texts = [getattr(block, "text", "") for msg in request.messages for block in msg.content]
    assert texts.index("context-a") < texts.index("context-b")
    context_event = next(event for event in events if event.type == "context_build")
    assert context_event.metadata == {"source": "static", "a": 0, "b": 0}


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

    provider = ScriptedProvider([TextTurn("done")])
    agent = _agent(provider, hooks=[_context_hook(BudgetBuilder())])
    session = await agent.session()
    events = await _drain(session)

    texts = [
        getattr(block, "text", "") for msg in provider.requests[0].messages for block in msg.content
    ]
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

    provider = ScriptedProvider([TextTurn("done")])
    agent = _agent(provider, hooks=[_context_hook(SelectingBuilder())])
    session = await agent.session()
    await _drain(session)

    assert [tool["name"] for tool in provider.requests[0].tools] == ["SearchDocs"]
    assert sorted(tool.name for tool in agent.tools.list()) == ["SearchDocs", "WriteNote"]


@pytest.mark.asyncio
async def test_context_builder_reruns_after_compaction_retry():
    from linch import ContextBuildResult, ContextLengthError
    from linch.types import Message, TextBlock

    class RetryProvider(ScriptedProvider):
        def __init__(self):
            super().__init__([TextTurn("done")])

        async def stream(self, req):
            if not self.requests:
                self.requests.append(req)
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
    agent = _agent(provider, hooks=[_context_hook(builder)])
    agent.compaction = NoopCompaction()
    session = await agent.session()
    events = await _drain(session)

    assert builder.calls == 2
    assert [event.type for event in events].count("context_build") == 2
    texts = [
        getattr(block, "text", "")
        for msg in provider.requests[-1].messages
        for block in msg.content
    ]
    assert "fresh-context-2" in texts


def _msg(text: str):
    from linch.types import Message, TextBlock

    return Message(role="user", content=[TextBlock(text=text)])


def test_estimator_budget_trims_oldest_first_under_budget():
    from linch import ContextBudget, ContextBuildResult
    from linch.context.builder import apply_context_budget

    calls: list[tuple[list[object], str]] = []

    def estimator(messages, model):
        calls.append((messages, model))
        return len(messages)

    result = ContextBuildResult(
        messages=[_msg(f"m{i}") for i in range(10)], budget=ContextBudget(max_tokens=3)
    )
    out = apply_context_budget(result, estimator=estimator, model="gpt-5")
    kept = [getattr(block, "text", "") for msg in out.messages for block in msg.content]
    assert out.budget.used_tokens <= 3
    assert out.budget.trimmed is True
    assert kept == ["m7", "m8", "m9"]
    assert calls and all(model == "gpt-5" for _, model in calls)


def test_estimator_budget_invocations_are_linear():
    from linch import ContextBudget, ContextBuildResult
    from linch.context.builder import apply_context_budget

    work = {"messages_seen": 0}

    def estimator(messages, model):
        work["messages_seen"] += len(messages)
        return len(messages)

    n = 50
    result = ContextBuildResult(
        messages=[_msg(f"m{i}") for i in range(n)], budget=ContextBudget(max_tokens=1)
    )
    apply_context_budget(result, estimator=estimator, model="gpt-5")
    assert work["messages_seen"] <= 3 * n
