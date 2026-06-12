from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest


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


def make_agent(registry: Any, middleware: Any = None) -> SimpleNamespace:
    from linch.hooks import ToolMiddlewareHook
    from linch.permissions import PermissionEngine

    hooks = [] if middleware is None else [ToolMiddlewareHook(middleware)]
    return SimpleNamespace(
        cwd=".",
        tools=registry,
        permission_engine=PermissionEngine(mode="skip-dangerous"),
        max_tool_concurrency=1,
        tool_concurrency=1,
        hooks=hooks,
    )


def make_session() -> SimpleNamespace:
    return SimpleNamespace(
        id="s1",
        store=None,
        active_run_id="run-1",
        tools_override=None,
        current_turn_allowed_tools=None,
        run_deps={"tenant": "unit"},
    )


async def collect_events(registry: Any, middleware: Any = None) -> list[Any]:
    from linch.abort import AbortContext
    from linch.scheduler import execute_tool_calls
    from linch.types import ToolUseBlock

    return [
        event
        async for event in execute_tool_calls(
            [ToolUseBlock(id="t1", name="Record", input={"value": "original"})],
            make_agent(registry, middleware),
            make_session(),
            AbortContext(),
            turn_index=3,
        )
    ]


@pytest.mark.asyncio
async def test_before_tool_call_rewrites_effective_input() -> None:
    from linch import ToolCallEndEvent, ToolCallStartEvent, ToolRegistry

    tool = RecordingTool()
    registry = ToolRegistry()
    registry.add(tool)

    events = await collect_events(registry, RewriteInputMiddleware())

    start = next(e for e in events if isinstance(e, ToolCallStartEvent))
    end = next(e for e in events if isinstance(e, ToolCallEndEvent))
    assert start.input == {"value": "rewritten"}
    assert start.summary == "Record(rewritten)"
    assert tool.inputs == [{"value": "rewritten"}]
    assert end.is_error is False


@pytest.mark.asyncio
async def test_before_tool_call_can_block_with_bracketed_events() -> None:
    from linch import ToolCallEndEvent, ToolCallStartEvent, ToolRegistry

    tool = RecordingTool()
    registry = ToolRegistry()
    registry.add(tool)

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
    from linch import ToolCallEndEvent, ToolRegistry

    registry = ToolRegistry()
    registry.add(RecordingTool(content="contains secret"))

    events = await collect_events(registry, RedactResultMiddleware())

    end = next(e for e in events if isinstance(e, ToolCallEndEvent))
    assert end.result == "contains [redacted]"
    assert end.tool_result is not None
    assert end.tool_result.content == "contains [redacted]"


@pytest.mark.asyncio
async def test_multiple_middleware_run_in_order() -> None:
    from linch import ToolCallEndEvent, ToolCallStartEvent, ToolRegistry

    registry = ToolRegistry()
    registry.add(RecordingTool(content="base"))

    events = await collect_events(registry, [AppendMiddleware("-a"), AppendMiddleware("-b")])

    start = next(e for e in events if isinstance(e, ToolCallStartEvent))
    end = next(e for e in events if isinstance(e, ToolCallEndEvent))
    assert start.input == {"value": "original-a-b"}
    assert end.result == "base-a-b"


@pytest.mark.asyncio
async def test_no_middleware_preserves_existing_behavior() -> None:
    from linch import ToolCallEndEvent, ToolCallStartEvent, ToolRegistry

    tool = RecordingTool()
    registry = ToolRegistry()
    registry.add(tool)

    events = await collect_events(registry)

    start = next(e for e in events if isinstance(e, ToolCallStartEvent))
    end = next(e for e in events if isinstance(e, ToolCallEndEvent))
    assert start.input == {"value": "original"}
    assert tool.inputs == [{"value": "original"}]
    assert end.result == "ok"


@pytest.mark.asyncio
async def test_rewritten_result_enters_provider_history() -> None:
    from linch import Agent, ToolResult, ToolResultBlock, Usage
    from linch.hooks import ToolMiddlewareHook
    from linch.providers import BaseProvider
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools

    class SecretTool:
        name = "Secret"
        description = "Returns a secret."
        input_schema = {"type": "object", "properties": {}}
        scope = "read"
        parallel = False

        def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
            return raw

        def summarize(self, input: dict[str, Any]) -> str:
            return "Secret"

        async def execute(self, input: dict[str, Any], ctx: Any) -> ToolResult:
            return ToolResult(content="raw secret")

    class FakeProvider(BaseProvider):
        def __init__(self) -> None:
            self.requests: list[Any] = []

        def context_window(self, model: str) -> int:
            return 128_000

        async def stream(self, req: Any) -> Any:
            self.requests.append(req)
            yield {"type": "message_start", "model": req.model}
            if len(self.requests) == 1:
                yield {"type": "tool_use_start", "id": "t1", "name": "Secret"}
                yield {"type": "tool_use_input_delta", "id": "t1", "json_delta": "{}"}
                yield {"type": "tool_use_end", "id": "t1"}
                yield {"type": "message_end", "stop_reason": "tool_use", "usage": Usage()}
            else:
                yield {"type": "text_delta", "text": "done"}
                yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}

    provider = FakeProvider()
    agent = Agent(
        model="gpt-5",
        provider=provider,
        tools=empty_tools(SecretTool()),
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
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
