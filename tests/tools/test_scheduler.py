from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from linch.abort import AbortContext
from linch.events import ToolCallEndEvent
from linch.permissions import PermissionEngine
from linch.scheduler import execute_tool_calls
from linch.tools import (
    Citation,
    ResourceAccess,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolScope,
)
from linch.types import ToolUseBlock
from tests.support.scheduler import scheduler_agent, scheduler_session


class Recorder:
    def __init__(self) -> None:
        self.running = 0
        self.max_running = 0
        self.timeline: list[tuple[str, str, float, int]] = []

    def start(self, name: str) -> None:
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        self.timeline.append(("start", name, time.perf_counter(), self.running))

    def end(self, name: str) -> None:
        self.timeline.append(("end", name, time.perf_counter(), self.running))
        self.running -= 1

    def started_before_end(self, later: str, earlier: str) -> bool:
        later_start = next(
            t for kind, name, t, _ in self.timeline if kind == "start" and name == later
        )
        earlier_end = next(
            t for kind, name, t, _ in self.timeline if kind == "end" and name == earlier
        )
        return later_start < earlier_end


class TimedTool:
    description = "Timed test tool."
    input_schema = {"type": "object", "properties": {"resource": {"type": "string"}}}

    def __init__(
        self,
        name: str,
        recorder: Recorder,
        *,
        scope: ToolScope = "read",
        parallel: bool = True,
        delay: float = 0.02,
        output: str | None = None,
    ) -> None:
        self.name = name
        self.recorder = recorder
        self.scope: ToolScope = scope
        self.parallel = parallel
        self.delay = delay
        self.output = output or name

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        return raw

    def summarize(self, input: dict[str, Any]) -> str:
        return self.name

    def resources(self, input: dict[str, Any]) -> list[ResourceAccess]:
        resource = input.get("resource")
        if not isinstance(resource, str):
            return []
        mode = "read" if self.scope == "read" else "write"
        return [ResourceAccess(resource=resource, mode=mode)]

    async def execute(self, input: dict[str, Any], ctx: ToolContext) -> ToolResult:
        self.recorder.start(self.name)
        try:
            await asyncio.sleep(self.delay)
            return ToolResult(content=self.output, summary=self.name)
        finally:
            self.recorder.end(self.name)


class RichTool:
    name = "Rich"
    description = "Returns a rich ToolResult."
    input_schema = {"type": "object", "properties": {}}
    scope = "read"
    parallel = True

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        return raw

    def summarize(self, input: dict[str, Any]) -> str:
        return self.name

    def resources(self, input: dict[str, Any]) -> list[ResourceAccess]:
        return []

    async def execute(self, input: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(
            content="rich content",
            summary="rich summary",
            metadata={"source": "unit", "rank": 1},
            citations=[
                Citation(
                    id="c1",
                    source="doc://1",
                    label="Doc 1",
                    chunk="chunk",
                    score=0.75,
                    metadata={"page": 4},
                )
            ],
            attachments=[object()],
            truncated=True,
        )


class InvalidTool:
    name = "Invalid"
    description = "Raises during validation."
    input_schema = {"type": "object", "properties": {}}
    scope = "read"
    parallel = True

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        raise ValueError("bad input")

    def summarize(self, input: dict[str, Any]) -> str:
        return self.name

    async def execute(self, input: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(content="should not run")


async def run_calls(registry: ToolRegistry, blocks: list[ToolUseBlock], *, limit: int = 8):
    events = [
        event
        async for event in execute_tool_calls(
            blocks,
            scheduler_agent(registry, max_tool_concurrency=limit),
            scheduler_session(),
            AbortContext(),
        )
    ]
    return events


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("names", "scopes", "resources", "limit", "expected_max"),
    [
        (("ReadA", "ReadB"), ("read", "read"), ("file:a", "file:b"), 8, 2),
        (("WriteA", "WriteB"), ("write", "write"), ("file:a", "file:b"), 8, 1),
        (("ReadA", "WriteA"), ("read", "write"), ("file:a", "file:a"), 8, 1),
        (
            ("ReadA", "ReadB", "ReadC"),
            ("read", "read", "read"),
            ("file:a", "file:b", "file:c"),
            2,
            2,
        ),
    ],
    ids=["read-parallel", "write-serial", "shared-resource", "concurrency-cap"],
)
async def test_tool_batching_respects_scope_resource_and_limit(
    names: tuple[str, ...],
    scopes: tuple[ToolScope, ...],
    resources: tuple[str, ...],
    limit: int,
    expected_max: int,
) -> None:
    recorder = Recorder()
    registry = ToolRegistry()
    for name, scope in zip(names, scopes, strict=True):
        registry.register(TimedTool(name, recorder, scope=scope, parallel=True))

    await run_calls(
        registry,
        [
            ToolUseBlock(id=str(i), name=name, input={"resource": resource})
            for i, (name, resource) in enumerate(zip(names, resources, strict=True))
        ],
        limit=limit,
    )

    assert recorder.max_running == expected_max
    if names == ("ReadA", "ReadB"):
        assert recorder.started_before_end("ReadB", "ReadA")


def test_agent_accepts_max_tool_concurrency_option() -> None:
    from linch import Agent
    from tests.support.providers import ScriptedProvider

    agent = Agent(model="dummy", provider=ScriptedProvider([]), max_tool_concurrency=3)

    assert agent.max_tool_concurrency == 3
    assert agent.tool_concurrency == 3


@pytest.mark.asyncio
async def test_results_keep_original_tool_call_order() -> None:
    recorder = Recorder()
    registry = ToolRegistry()
    registry.register(TimedTool("Slow", recorder, delay=0.03, output="slow"))
    registry.register(TimedTool("Fast", recorder, delay=0.005, output="fast"))

    events = await run_calls(
        registry,
        [
            ToolUseBlock(id="slow", name="Slow", input={"resource": "file:slow"}),
            ToolUseBlock(id="fast", name="Fast", input={"resource": "file:fast"}),
        ],
    )

    end_events = [e for e in events if isinstance(e, ToolCallEndEvent)]
    assert [e.tool_use_id for e in end_events] == ["slow", "fast"]
    assert [e.result for e in end_events] == ["slow", "fast"]


@pytest.mark.asyncio
async def test_tool_call_end_preserves_rich_tool_result() -> None:
    registry = ToolRegistry()
    registry.register(RichTool())

    events = await run_calls(registry, [ToolUseBlock(id="rich", name="Rich", input={})])

    end = next(e for e in events if isinstance(e, ToolCallEndEvent))
    assert end.result == "rich content"
    assert end.tool_result is not None
    assert end.tool_result.content == "rich content"
    assert end.tool_result.summary == "rich summary"
    assert end.tool_result.metadata == {"source": "unit", "rank": 1}
    assert end.tool_result.citations[0].source == "doc://1"
    assert end.tool_result.attachments
    assert end.tool_result.truncated is True


@pytest.mark.asyncio
async def test_immediate_validation_and_permission_errors_have_tool_result() -> None:
    registry = ToolRegistry()
    registry.register(InvalidTool())
    recorder = Recorder()
    registry.register(TimedTool("Denied", recorder, scope="write", parallel=False))

    validation_events = await run_calls(
        registry,
        [ToolUseBlock(id="invalid", name="Invalid", input={})],
    )
    validation_end = next(e for e in validation_events if isinstance(e, ToolCallEndEvent))
    assert validation_end.tool_result is not None
    assert validation_end.tool_result.is_error is True
    assert validation_end.tool_result.content == validation_end.result
    assert "bad input" in validation_end.tool_result.content

    unknown_events = await run_calls(
        registry,
        [ToolUseBlock(id="missing", name="Missing", input={})],
    )
    unknown_end = next(e for e in unknown_events if isinstance(e, ToolCallEndEvent))
    assert unknown_end.tool_result is not None
    assert unknown_end.tool_result.is_error is True
    assert "not registered" in unknown_end.tool_result.content

    denied_events = [
        event
        async for event in execute_tool_calls(
            [ToolUseBlock(id="denied", name="Denied", input={})],
            scheduler_agent(registry, permission_engine=PermissionEngine(mode="default")),
            scheduler_session(current_turn_permission_decisions=None),
            AbortContext(),
        )
    ]
    denied_end = next(e for e in denied_events if isinstance(e, ToolCallEndEvent))
    assert denied_end.tool_result is not None
    assert denied_end.tool_result.is_error is True
    assert "Permission denied" in denied_end.tool_result.content


@pytest.mark.asyncio
async def test_fast_first_tool_end_emits_before_slow_second_completes() -> None:
    """No full-batch barrier: a fast provider-first tool's end emits while a slow
    later tool is still running (Phase 2.4)."""
    gate = asyncio.Event()

    class _Gated:
        input_schema = {"type": "object", "properties": {}}
        scope: ToolScope = "read"
        parallel = True

        def __init__(self, name: str, *, wait: bool) -> None:
            self.name = name
            self.description = "d"
            self._wait = wait

        def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
            return raw

        def summarize(self, input: dict[str, Any]) -> str:
            return self.name

        def resources(self, input: dict[str, Any]) -> list[ResourceAccess]:
            return []

        async def execute(self, input: dict[str, Any], ctx: ToolContext) -> ToolResult:
            if self._wait:
                await gate.wait()
            return ToolResult(content=self.name)

    registry = ToolRegistry()
    registry.register(_Gated("Fast", wait=False))
    registry.register(_Gated("Slow", wait=True))

    iterator = execute_tool_calls(
        [
            ToolUseBlock(id="fast", name="Fast", input={}),
            ToolUseBlock(id="slow", name="Slow", input={}),
        ],
        scheduler_agent(registry),
        scheduler_session(),
        AbortContext(),
    ).__aiter__()

    seen: list[tuple[str, str | None]] = []
    while True:
        event = await asyncio.wait_for(iterator.__anext__(), timeout=1.0)
        seen.append((event.type, getattr(event, "tool_use_id", None)))
        if event.type == "tool_call_end" and getattr(event, "tool_use_id", None) == "fast":
            break

    # Fast's end arrived without the slow tool being released — no barrier.
    assert not gate.is_set()
    assert ("tool_call_end", "slow") not in seen

    gate.set()
    rest = [event async for event in iterator]
    assert any(
        e.type == "tool_call_end" and getattr(e, "tool_use_id", None) == "slow" for e in rest
    )
