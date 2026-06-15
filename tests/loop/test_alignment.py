from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest


class _ToolThenTextProvider:
    id = "alignment-test"

    def __init__(self) -> None:
        self.calls = 0
        self.requests: list[Any] = []

    def context_window(self, model: str) -> int:
        return 128_000

    async def stream(self, req: Any) -> AsyncIterator[dict[str, Any]]:
        from linch.types import Usage

        self.calls += 1
        self.requests.append(req)
        yield {"type": "message_start", "model": req.model}
        if self.calls == 1:
            yield {"type": "tool_use_start", "id": "call_1", "name": "Wait"}
            yield {"type": "tool_use_input_delta", "id": "call_1", "json_delta": "{}"}
            yield {"type": "tool_use_end", "id": "call_1"}
            yield {"type": "message_end", "stop_reason": "tool_use", "usage": Usage()}
        else:
            yield {"type": "text_delta", "text": "done"}
            yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}


class _WaitTool:
    name = "Wait"
    description = "Wait until released."
    input_schema = {"type": "object", "properties": {}}
    scope = "read"
    parallel = False

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        return {}

    def summarize(self, input: dict[str, Any]) -> str:
        return "Wait"

    async def execute(self, input: dict[str, Any], ctx: Any) -> Any:
        from linch import ToolResult

        self.started.set()
        await self.release.wait()
        return ToolResult(content="waited")


def _agent(provider: Any, tool: Any, *, hooks: Any = None) -> Any:
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools

    return Agent(
        model="test-model",
        provider=provider,
        tools=empty_tools(tool),
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        result_offload=None,
        hooks=hooks,
        loop_guard=None,
    )


def _text_messages(req: Any) -> list[str]:
    return [
        block.text
        for message in req.messages
        for block in message.content
        if getattr(block, "type", None) == "text"
    ]


@pytest.mark.asyncio
async def test_idle_align_rejects() -> None:
    from linch.errors import ConfigError

    provider = _ToolThenTextProvider()
    tool = _WaitTool()
    session = await _agent(provider, tool).session()

    with pytest.raises(ConfigError):
        await session.align("not running")


@pytest.mark.asyncio
async def test_alignment_injects_user_message_before_next_model_turn() -> None:
    from linch.events import ResultEvent, UserEvent

    provider = _ToolThenTextProvider()
    tool = _WaitTool()
    session = await _agent(provider, tool).session()
    events: list[Any] = []

    async def collect() -> None:
        async for event in session.run("start"):
            events.append(event)

    run_task = asyncio.create_task(collect())
    await tool.started.wait()
    align_task = asyncio.create_task(session.align("please adjust course"))
    tool.release.set()
    await run_task
    await align_task

    alignment_events = [
        event for event in events if isinstance(event, UserEvent) and event.subtype == "alignment"
    ]
    assert len(alignment_events) == 1
    assert provider.calls == 2
    assert "please adjust course" in _text_messages(provider.requests[1])
    assert isinstance(events[-1], ResultEvent)
    assert events[-1].subtype == "success"


@pytest.mark.asyncio
async def test_interrupt_stops_before_next_model_turn() -> None:
    from linch.events import ResultEvent

    provider = _ToolThenTextProvider()
    tool = _WaitTool()
    session = await _agent(provider, tool).session()
    events: list[Any] = []

    async def collect() -> None:
        async for event in session.run("start"):
            events.append(event)

    run_task = asyncio.create_task(collect())
    await tool.started.wait()
    session.interrupt()
    tool.release.set()
    await run_task

    assert provider.calls == 1
    result = next(event for event in reversed(events) if isinstance(event, ResultEvent))
    assert result.subtype == "interrupted"
    assert result.stop_reason == "interrupted"


@pytest.mark.asyncio
async def test_interrupt_rejects_pending_alignment() -> None:
    from linch.errors import ConfigError

    provider = _ToolThenTextProvider()
    tool = _WaitTool()
    session = await _agent(provider, tool).session()

    async def collect() -> list[Any]:
        return [event async for event in session.run("start")]

    run_task = asyncio.create_task(collect())
    await tool.started.wait()
    align_task = asyncio.create_task(session.align("late change"))
    session.interrupt()
    tool.release.set()
    await run_task
    with pytest.raises(ConfigError):
        await align_task


@pytest.mark.asyncio
async def test_alignment_runs_user_prompt_hooks_with_source() -> None:
    from linch import HookResult

    sources: list[str] = []

    class Hooks:
        def on_user_prompt_submit(self, ctx: Any) -> Any:
            sources.append(ctx.source)
            if ctx.source == "align":
                return HookResult.mutate(prompt=f"{ctx.prompt} hooked")
            return None

    provider = _ToolThenTextProvider()
    tool = _WaitTool()
    session = await _agent(provider, tool, hooks=[Hooks()]).session()

    async def collect() -> list[Any]:
        return [event async for event in session.run("start")]

    run_task = asyncio.create_task(collect())
    await tool.started.wait()
    align_task = asyncio.create_task(session.align("change"))
    tool.release.set()
    await run_task
    await align_task

    assert sources == ["run", "align"]
    assert "change hooked" in _text_messages(provider.requests[1])


def test_user_event_subtype_serializes() -> None:
    from linch.events import UserEvent, event_from_dict, event_to_dict
    from linch.types import Message, TextBlock

    event = UserEvent(
        message=Message(role="user", content=[TextBlock(text="hi")]),
        subtype="alignment",
    )
    rebuilt = event_from_dict(json.loads(json.dumps(event_to_dict(event))))
    assert isinstance(rebuilt, UserEvent)
    assert rebuilt.subtype == "alignment"


@pytest.mark.asyncio
async def test_drain_alignment_surfaces_unexpected_failure_as_error_event() -> None:
    """An unexpected fault applying a align must emit an ErrorEvent, not vanish."""
    from linch.events import ErrorEvent
    from linch.loop.runner import _drain_alignment
    from linch.session import AlignmentEntry

    class _FakeAgent:
        run_store = None

    class _FakeSession:
        def __init__(self) -> None:
            self.agent = _FakeAgent()
            self.alignment_queue: list[AlignmentEntry] = []

        async def append(self, messages: Any) -> None:  # pragma: no cover - unreached
            pass

    session = _FakeSession()
    fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    session.alignment_queue.append(AlignmentEntry(prompt="hi", images=None, future=fut))

    async def boom_dispatch(prompt, images, source):
        raise RuntimeError("dispatch exploded")

    events = [ev async for ev in _drain_alignment(session, "run-1", boom_dispatch)]

    assert any(isinstance(ev, ErrorEvent) for ev in events)
    assert fut.done() and isinstance(fut.exception(), RuntimeError)


@pytest.mark.asyncio
async def test_align_times_out_when_run_never_reaches_boundary() -> None:
    """align(timeout_s=...) must not block forever if the run never drains it."""
    from linch.errors import ConfigError

    provider = _ToolThenTextProvider()
    tool = _WaitTool()
    session = await _agent(provider, tool).session()
    session._active = True  # simulate an active run that never reaches a boundary
    try:
        with pytest.raises(ConfigError, match="timed out"):
            await session.align("change", timeout_s=0.05)
        # The timed-out entry is dropped so it cannot inject into a later turn.
        assert session.alignment_queue == []
    finally:
        session._active = False
