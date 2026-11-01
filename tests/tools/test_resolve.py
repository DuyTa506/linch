from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from linch.abort import AbortContext
from linch.events import ToolCallEndEvent, ToolCallStartEvent
from linch.permissions import PermissionEngine
from linch.scheduler import execute_tool_calls
from linch.tools import ToolContext, ToolRegistry, ToolResult
from linch.types import ToolUseBlock
from tests.support.scheduler import scheduler_agent, scheduler_session


class _RecordingTool:
    name = "Deferred"
    description = "A tool used to test the resolve seam."
    input_schema = {"type": "object", "properties": {"value": {"type": "string"}}}
    scope = "read"
    parallel = False

    def __init__(self, *, fail_validation: bool = False) -> None:
        self.fail_validation = fail_validation
        self.validations = 0
        self.executions = 0

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        self.validations += 1
        if self.fail_validation:
            raise AssertionError("resolve veto must run before validation")
        return dict(raw)

    def summarize(self, input: dict[str, Any]) -> str:
        return f"Deferred({input.get('value', '')})"

    async def execute(self, input: dict[str, Any], ctx: ToolContext) -> ToolResult:
        self.executions += 1
        return ToolResult(content="executed", summary="Deferred")


class _CountingPermissionEngine(PermissionEngine):
    def __init__(self) -> None:
        super().__init__(mode="skip-dangerous")
        self.evaluations = 0
        self.resolutions = 0

    def evaluate(self, call: Any):
        self.evaluations += 1
        return super().evaluate(call)

    async def resolve(self, call: Any, signal: Any):
        self.resolutions += 1
        return await super().resolve(call, signal)


class _Hook:
    def __init__(self) -> None:
        self.calls = 0

    def on_pre_tool_use(self, ctx: Any) -> None:
        self.calls += 1


def _agent(
    tool: Any,
    *,
    context: Any = None,
    permission_engine: PermissionEngine | None = None,
    hooks: list[Any] | None = None,
) -> SimpleNamespace:
    from linch import Context

    registry = ToolRegistry()
    registry.register(tool)
    return scheduler_agent(
        registry,
        permission_engine=permission_engine or PermissionEngine(mode="skip-dangerous"),
        max_tool_concurrency=1,
        hooks=list(hooks or []),
        context=context or Context(),
    )


def _session() -> SimpleNamespace:
    return scheduler_session(session_id="session", run_id="run")


async def _events(agent: Any, session: Any | None = None) -> list[Any]:
    return [
        event
        async for event in execute_tool_calls(
            [ToolUseBlock(id="call", name="Deferred", input={"value": "raw"})],
            agent,
            session or _session(),
            AbortContext(),
        )
    ]


def _tool_event_shape(events: list[Any]) -> list[dict[str, Any]]:
    """Normalize ToolCall events to deterministic provider-visible fields."""
    shaped: list[dict[str, Any]] = []
    for event in events:
        if isinstance(event, ToolCallStartEvent):
            shaped.append(
                {
                    "type": event.type,
                    "tool_use_id": event.tool_use_id,
                    "tool_name": event.tool_name,
                    "input": event.input,
                    "summary": event.summary,
                }
            )
        elif isinstance(event, ToolCallEndEvent):
            shaped.append(
                {
                    "type": event.type,
                    "tool_use_id": event.tool_use_id,
                    "tool_name": event.tool_name,
                    "result": event.result,
                    "is_error": event.is_error,
                }
            )
    return shaped


@pytest.mark.asyncio
async def test_resolve_veto_precedes_validation_hooks_and_permissions() -> None:
    tool = _RecordingTool(fail_validation=True)
    permission = _CountingPermissionEngine()
    hook = _Hook()
    agent = _agent(tool, permission_engine=permission, hooks=[hook])
    session = _session()
    seen: list[tuple[str, dict[str, Any], Any]] = []

    async def resolve(
        tool_name: str, raw_input: dict[str, Any], current_session: Any
    ) -> str | None:
        seen.append((tool_name, raw_input, current_session))
        return "deferred; call tool_search first"

    agent.context.on("tools/resolve", resolve)
    events = await _events(agent, session)

    assert seen == [("Deferred", {"value": "raw"}, session)]
    ends = [event for event in events if isinstance(event, ToolCallEndEvent)]
    assert len(ends) == 1
    assert ends[0].is_error is True
    assert "tool_search" in ends[0].result
    assert tool.validations == 0
    assert tool.executions == 0
    assert hook.calls == 0
    assert permission.evaluations == 0
    assert permission.resolutions == 0


@pytest.mark.asyncio
async def test_resolve_listeners_are_serial_and_disposal_restores_execution() -> None:
    tool = _RecordingTool()
    agent = _agent(tool)
    session = _session()
    order: list[str] = []

    async def first(*args: Any) -> None:
        order.append("first")

    async def veto(*args: Any) -> str:
        order.append("veto")
        return "blocked by second listener"

    async def unreachable(*args: Any) -> None:
        order.append("unreachable")

    agent.context.on("tools/resolve", first)
    disposer = agent.context.on("tools/resolve", veto)
    agent.context.on("tools/resolve", unreachable)

    blocked = await _events(agent, session)
    blocked_end = next(event for event in blocked if isinstance(event, ToolCallEndEvent))
    assert order == ["first", "veto"]
    assert blocked_end.is_error is True
    assert tool.executions == 0

    await disposer.dispose()
    order.clear()
    allowed = await _events(agent, session)
    allowed_end = next(event for event in allowed if isinstance(event, ToolCallEndEvent))
    assert order == ["first", "unreachable"]
    assert allowed_end.is_error is False
    assert tool.executions == 1


@pytest.mark.asyncio
async def test_no_resolve_listener_skips_serial_and_preserves_tool_events() -> None:
    baseline_tool = _RecordingTool()
    baseline = await _events(_agent(baseline_tool))

    class SpyBus:
        def __init__(self) -> None:
            self.serial_calls = 0

        def has_listeners(self, name: str) -> bool:
            return False

        async def serial(self, name: str, *args: Any) -> Any:
            self.serial_calls += 1
            raise AssertionError("serial must not run without a resolve listener")

    spy = SpyBus()
    from linch import Context

    context = Context()
    context.events = spy
    candidate_tool = _RecordingTool()
    candidate = await _events(_agent(candidate_tool, context=context))

    assert spy.serial_calls == 0
    assert _tool_event_shape(candidate) == _tool_event_shape(baseline)


@pytest.mark.asyncio
async def test_shared_parent_bus_listener_filters_by_session() -> None:
    from linch import Context

    parent = Context()
    agent_a = _agent(_RecordingTool(), context=parent.scope("agent-a"))
    agent_b = _agent(_RecordingTool(), context=parent.scope("agent-b"))
    session_a = _session()
    session_b = _session()
    session_a.id = "a"
    session_b.id = "b"

    async def resolve(_name: str, _input: dict[str, Any], current_session: Any) -> str | None:
        if current_session is session_a:
            return "agent-a deferred this call"
        return None

    agent_a.context.on("tools/resolve", resolve)
    events_a = await _events(agent_a, session_a)
    events_b = await _events(agent_b, session_b)

    end_a = next(event for event in events_a if isinstance(event, ToolCallEndEvent))
    end_b = next(event for event in events_b if isinstance(event, ToolCallEndEvent))
    assert end_a.is_error is True
    assert end_b.is_error is False
