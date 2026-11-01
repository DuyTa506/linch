from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from tests.support.agents import make_agent as support_make_agent
from tests.support.providers import ScriptedProvider, TextTurn, ToolUseTurn
from tests.support.scheduler import scheduler_agent, scheduler_session
from tests.support.tools import StubTool, tool_registry


class RecordingTool:
    name = "Record"
    description = "Records input and returns configured content."
    input_schema = {"type": "object", "properties": {"value": {"type": "string"}}}
    scope = "read"
    parallel = False

    def __init__(self, content: str = "ok") -> None:
        self.inputs: list[dict[str, Any]] = []
        self.content = content

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        return dict(raw)

    def summarize(self, input: dict[str, Any]) -> str:
        return f"Record({input.get('value', '')})"

    async def execute(self, input: dict[str, Any], ctx: Any) -> Any:
        from linch import ToolResult

        self.inputs.append(dict(input))
        return ToolResult(content=self.content)


class RewriteInputMiddleware:
    def before_tool_call(self, call: Any, ctx: Any) -> Any:
        from linch import ToolCallMiddlewareResult

        return ToolCallMiddlewareResult(input={**call.input, "value": "rewritten"})


class BlockMiddleware:
    async def before_tool_call(self, call: Any, ctx: Any) -> Any:
        from linch import ToolCallMiddlewareResult

        return ToolCallMiddlewareResult(error=f"blocked {ctx.tool_name}")


class RedactResultMiddleware:
    def after_tool_result(self, call: Any, result: Any, ctx: Any) -> Any:
        return replace(result, content=result.content.replace("secret", "[redacted]"))


class AppendMiddleware:
    def __init__(self, text: str) -> None:
        self.text = text

    def before_tool_call(self, call: Any, ctx: Any) -> Any:
        from linch import ToolCallMiddlewareResult

        return ToolCallMiddlewareResult(
            input={**call.input, "value": call.input["value"] + self.text}
        )

    def after_tool_result(self, call: Any, result: Any, ctx: Any) -> Any:
        return replace(result, content=result.content + self.text)


async def collect_events(registry: Any, middleware: Any = None) -> list[Any]:
    from linch.abort import AbortContext
    from linch.hooks import ToolMiddlewareHook
    from linch.scheduler import execute_tool_calls
    from linch.types import ToolUseBlock

    hooks = [] if middleware is None else [ToolMiddlewareHook(middleware)]
    return [
        event
        async for event in execute_tool_calls(
            [ToolUseBlock(id="t1", name="Record", input={"value": "original"})],
            scheduler_agent(
                registry,
                max_tool_concurrency=1,
                tool_concurrency=1,
                hooks=hooks,
            ),
            scheduler_session(run_id="run-1", run_deps={"tenant": "unit"}),
            AbortContext(),
            turn_index=3,
        )
    ]


@pytest.mark.asyncio
async def test_before_tool_call_rewrites_effective_input() -> None:
    from linch import ToolCallEndEvent, ToolCallStartEvent

    tool = RecordingTool()
    registry = tool_registry(tool)

    events = await collect_events(registry, RewriteInputMiddleware())

    start = next(e for e in events if isinstance(e, ToolCallStartEvent))
    end = next(e for e in events if isinstance(e, ToolCallEndEvent))
    assert start.input == {"value": "rewritten"}
    assert start.summary == "Record(rewritten)"
    assert tool.inputs == [{"value": "rewritten"}]
    assert end.is_error is False


@pytest.mark.asyncio
async def test_before_tool_call_can_block_with_bracketed_events() -> None:
    from linch import ToolCallEndEvent, ToolCallStartEvent

    tool = RecordingTool()
    registry = tool_registry(tool)

    events = await collect_events(registry, BlockMiddleware())

    assert len([e for e in events if isinstance(e, ToolCallStartEvent)]) == 1
    end = next(e for e in events if isinstance(e, ToolCallEndEvent))
    assert end.is_error is True
    assert end.tool_result is not None
    assert end.tool_result.is_error is True
    assert end.result == "blocked Record"
    assert tool.inputs == []


@pytest.mark.asyncio
async def test_after_tool_result_rewrites_event_result() -> None:
    from linch import ToolCallEndEvent

    registry = tool_registry(RecordingTool(content="contains secret"))

    events = await collect_events(registry, RedactResultMiddleware())

    end = next(e for e in events if isinstance(e, ToolCallEndEvent))
    assert end.result == "contains [redacted]"
    assert end.tool_result is not None
    assert end.tool_result.content == "contains [redacted]"


@pytest.mark.asyncio
async def test_multiple_middleware_run_in_order() -> None:
    from linch import ToolCallEndEvent, ToolCallStartEvent

    registry = tool_registry(RecordingTool(content="base"))

    events = await collect_events(registry, [AppendMiddleware("-a"), AppendMiddleware("-b")])

    start = next(e for e in events if isinstance(e, ToolCallStartEvent))
    end = next(e for e in events if isinstance(e, ToolCallEndEvent))
    assert start.input == {"value": "original-a-b"}
    assert end.result == "base-a-b"


@pytest.mark.asyncio
async def test_no_middleware_preserves_existing_behavior() -> None:
    from linch import ToolCallEndEvent, ToolCallStartEvent

    tool = RecordingTool()
    registry = tool_registry(tool)

    events = await collect_events(registry)

    start = next(e for e in events if isinstance(e, ToolCallStartEvent))
    end = next(e for e in events if isinstance(e, ToolCallEndEvent))
    assert start.input == {"value": "original"}
    assert tool.inputs == [{"value": "original"}]
    assert end.result == "ok"


@pytest.mark.asyncio
async def test_rewritten_result_enters_provider_history() -> None:
    from linch import ToolResultBlock
    from linch.hooks import ToolMiddlewareHook

    provider = ScriptedProvider([ToolUseTurn("Secret", {}), TextTurn("done")])
    agent = support_make_agent(
        provider=provider,
        model="gpt-5",
        tools=tool_registry(StubTool("Secret", output="raw secret")),
        run_store=None,
        hooks=[ToolMiddlewareHook(RedactResultMiddleware())],
    )
    session = await agent.session()

    async for _ in session.run("go"):
        pass

    second_request = provider.requests[1]
    tool_results = [
        block
        for message in second_request.messages
        for block in message.content
        if isinstance(block, ToolResultBlock)
    ]
    assert tool_results[0].content == "raw [redacted]"
