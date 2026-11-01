from __future__ import annotations

import asyncio
from typing import Any

from linch.abort import AbortContext
from linch.events import ToolProgressEvent
from linch.hooks import HookDispatcher
from linch.permissions import PermissionDecision, PermissionEngine
from linch.scheduler import ResolvedCall, _run_parallel_batch
from linch.tools import ToolContext, ToolRegistry, ToolResult
from linch.types import ToolUseBlock
from tests.support.scheduler import scheduler_agent, scheduler_session


class _QuickTool:
    description = "quick test tool"
    input_schema = {"type": "object"}
    scope = "read"
    parallel = True

    def __init__(self, name: str) -> None:
        self.name = name

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        return dict(raw)

    def summarize(self, input: dict[str, Any]) -> str:
        return self.name

    async def execute(self, input: dict[str, Any], ctx: ToolContext) -> ToolResult:
        await asyncio.sleep(0)
        return ToolResult(content=self.name)


def _call(tool: _QuickTool, call_id: str) -> ResolvedCall:
    block = ToolUseBlock(id=call_id, name=tool.name, input={})
    return ResolvedCall(
        id=call_id,
        block=block,
        tool=tool,
        input={},
        summary=tool.name,
        is_immediate_error=False,
    )


async def test_parallel_lane_closes_progress_generator_on_close(monkeypatch: Any) -> None:
    closed = asyncio.Event()

    class ProgressStream:
        def __init__(self) -> None:
            self.first = True

        def __aiter__(self) -> ProgressStream:
            return self

        async def __anext__(self) -> ToolProgressEvent:
            if self.first:
                self.first = False
                return ToolProgressEvent(
                    tool_use_id="first",
                    tool_name="First",
                    message="progress",
                )
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def aclose(self) -> None:
            closed.set()

    progress = ProgressStream()

    def progress_stream(*_: Any) -> ProgressStream:
        return progress

    import linch.scheduler as scheduler

    monkeypatch.setattr(scheduler, "_drain_parallel_progress_until", progress_stream)
    first = _QuickTool("First")
    second = _QuickTool("Second")
    registry = ToolRegistry()
    registry.register(first)
    registry.register(second)
    agent = scheduler_agent(registry, permission_engine=PermissionEngine(mode="skip-dangerous"))
    session = scheduler_session()
    calls = [_call(first, "first"), _call(second, "second")]
    lane = _run_parallel_batch(
        {
            "parallel": True,
            "calls": [
                (calls[0], 0, PermissionDecision(decision="allow")),
                (calls[1], 1, PermissionDecision(decision="allow")),
            ],
        },
        agent=agent,
        session=session,
        signal=AbortContext(),
        hook_dispatcher=HookDispatcher([]),
        middleware_errors={},
        turn_index=None,
    )

    assert isinstance(await lane.__anext__(), ToolProgressEvent) is False
    assert isinstance(await lane.__anext__(), ToolProgressEvent) is False
    assert isinstance(await lane.__anext__(), ToolProgressEvent)
    await lane.aclose()

    assert closed.is_set()
