"""Durable steering: the alignment queue survives crash/resume via RunCheckpoint.

`session.align()` intent enqueued mid-run must not be silently lost when the
process dies between enqueue and drain: every checkpoint save snapshots the
queue (`RunCheckpoint.pending_alignment`), and resume restores it so the drain
at the next turn boundary injects it — in order, at-least-once. A mid-turn
resume defers the drain past the re-processed tool batch so an injected user
message never lands between assistant(tool_use) and the tool-results message.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest


class _ToolThenTextProvider:
    id = "alignment-durability-test"

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


class _TextProvider:
    """Always answers with text; records every request."""

    id = "alignment-durability-text"

    def __init__(self, *, fail_on_call: bool = False) -> None:
        self.fail_on_call = fail_on_call
        self.requests: list[Any] = []

    def context_window(self, model: str) -> int:
        return 128_000

    async def stream(self, req: Any) -> AsyncIterator[dict[str, Any]]:
        from linch.types import Usage

        if self.fail_on_call:
            raise AssertionError("provider should not be called")
        self.requests.append(req)
        yield {"type": "message_start", "model": req.model}
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


class _QuickTool:
    name = "A"
    description = "Returns immediately."
    input_schema = {"type": "object", "properties": {}}
    scope = "read"
    parallel = False

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        return {}

    def summarize(self, input: dict[str, Any]) -> str:
        return "A"

    async def execute(self, input: dict[str, Any], ctx: Any) -> Any:
        from linch import ToolResult

        return ToolResult(content="A-ok")


def _agent(
    provider: Any,
    tool: Any,
    *,
    session_store: Any,
    run_store: Any,
    hooks: Any = None,
) -> Any:
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.tools.registry import empty_tools

    return Agent(
        model="test-model",
        provider=provider,
        tools=empty_tools(tool),
        permissions={"mode": "skip-dangerous"},
        session_store=session_store,
        run_store=run_store,
        hooks=hooks,
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        result_offload=None,
        loop_guard=None,
    )


def _memory_session_store():
    from linch.sessions import InMemorySessionStore

    return InMemorySessionStore()


def _memory_run_store():
    from linch.run_store import InMemoryRunStore

    return InMemoryRunStore()


def _text_messages(req: Any) -> list[str]:
    return [
        block.text
        for message in req.messages
        for block in message.content
        if getattr(block, "type", None) == "text"
    ]


def _alignment_events(events: list[Any]) -> list[Any]:
    from linch.events import UserEvent

    return [e for e in events if isinstance(e, UserEvent) and e.subtype == "alignment"]


async def _collect(iterator) -> list[Any]:
    return [event async for event in iterator]


async def _crash_after_tool_result(session, tool, prompt="start"):
    """Run until the tool-result user event (post `tool_results_appended` save),
    aligning "steer north" while the tool executes, then abandon the iterator —
    the crash idiom. Returns (run_id, align_task)."""
    events: list[Any] = []
    run_id = ""

    async def consume() -> None:
        nonlocal run_id
        async for event in session.run(prompt):
            events.append(event)
            if getattr(event, "type", None) == "system":
                run_id = event.run_id
            if (
                getattr(event, "type", None) == "user"
                and getattr(event, "subtype", "") == "tool_result"
            ):
                break

    consume_task = asyncio.create_task(consume())
    await tool.started.wait()
    align_task = asyncio.create_task(session.align("steer north"))
    await asyncio.sleep(0)  # let align() enqueue before the tool completes
    tool.release.set()
    await consume_task
    assert run_id
    return run_id, align_task


async def test_checkpoint_snapshots_pending_alignment() -> None:
    """An entry enqueued mid-run is durable at the next checkpoint save."""
    from linch.errors import ConfigError

    session_store = _memory_session_store()
    run_store = _memory_run_store()
    tool = _WaitTool()
    agent = _agent(_ToolThenTextProvider(), tool, session_store=session_store, run_store=run_store)
    session = await agent.session(id="s1")

    run_id, align_task = await _crash_after_tool_result(session, tool)

    run = await run_store.load_run(run_id)
    assert run is not None and run.checkpoint is not None
    assert run.checkpoint.pending_alignment == [{"prompt": "steer north", "images": None}]

    await session.aclose(force=True)
    with pytest.raises(ConfigError):
        await align_task


async def test_resume_restores_and_injects_pending_alignment() -> None:
    """A restored queue drains at the next turn boundary, exactly once."""
    from linch import RunOptions
    from linch.run_store import RunCheckpoint
    from linch.types import Usage

    session_store = _memory_session_store()
    run_store = _memory_run_store()
    provider = _TextProvider()
    agent = _agent(provider, _QuickTool(), session_store=session_store, run_store=run_store)
    session = await agent.session(id="s1")

    run = await run_store.create_run("s1", id="run-align")
    await run_store.save_checkpoint(
        run.id,
        RunCheckpoint(
            phase="turn_complete",
            prompt="continue",
            turn_index=0,
            total_usage=Usage(),
            pending_alignment=[{"prompt": "steer north", "images": None}],
        ),
    )

    events = await _collect(session.resume(run.id, RunOptions(allow_legacy_resume=True)))

    assert len(_alignment_events(events)) == 1
    assert provider.requests and "steer north" in _text_messages(provider.requests[0])
    assert events[-1].type == "result" and events[-1].subtype == "success"
    loaded = await run_store.load_run(run.id)
    assert loaded is not None and loaded.checkpoint is not None
    assert loaded.checkpoint.pending_alignment == []


async def test_crash_after_enqueue_before_drain_resume_injects() -> None:
    """Full crash/resume: a second agent on the same stores injects the intent."""
    from linch import RunOptions
    from linch.errors import ConfigError

    session_store = _memory_session_store()
    run_store = _memory_run_store()
    tool = _WaitTool()
    agent = _agent(_ToolThenTextProvider(), tool, session_store=session_store, run_store=run_store)
    session = await agent.session(id="s1")

    run_id, align_task = await _crash_after_tool_result(session, tool)
    await session.aclose(force=True)
    with pytest.raises(ConfigError):
        await align_task

    provider2 = _TextProvider()
    agent2 = _agent(provider2, _WaitTool(), session_store=session_store, run_store=run_store)
    resumed = await agent2.session(id="s1")
    events = await _collect(resumed.resume(run_id, RunOptions(allow_legacy_resume=True)))

    assert len(_alignment_events(events)) == 1
    assert provider2.requests and "steer north" in _text_messages(provider2.requests[0])
    assert events[-1].type == "result" and events[-1].subtype == "success"


async def test_resume_mid_tool_batch_defers_alignment_after_tool_results() -> None:
    """A mid-turn resume never injects between assistant(tool_use) and tool results."""
    from linch import RunOptions
    from linch.run_store import RunCheckpoint
    from linch.types import Message, TextBlock, ToolUseBlock, Usage

    session_store = _memory_session_store()
    run_store = _memory_run_store()
    provider = _TextProvider()
    agent = _agent(provider, _QuickTool(), session_store=session_store, run_store=run_store)
    session = await agent.session(id="s1")

    user_msg = Message(role="user", content=[TextBlock(text="start")])
    assistant_msg = Message(
        role="assistant", content=[ToolUseBlock(id="call-1", name="A", input={})]
    )
    await session.append([user_msg, assistant_msg])

    run = await run_store.create_run("s1", id="run-mid")
    await run_store.save_checkpoint(
        run.id,
        RunCheckpoint(
            phase="tool_batch_pending",
            prompt="start",
            turn_index=0,
            total_usage=Usage(),
            assistant_message=assistant_msg,
            pending_tool_blocks=[ToolUseBlock(id="call-1", name="A", input={})],
            pending_alignment=[{"prompt": "steer north", "images": None}],
        ),
    )

    events = await _collect(session.resume(run.id, RunOptions(allow_legacy_resume=True)))

    assert len(_alignment_events(events)) == 1
    assert events[-1].type == "result" and events[-1].subtype == "success"

    # Provider-order invariant: alignment lands strictly after the tool results.
    def _index(predicate) -> int:
        for idx, message in enumerate(session.full_history):
            if predicate(message):
                return idx
        raise AssertionError("message not found")

    assistant_idx = _index(
        lambda m: (
            m.role == "assistant" and any(getattr(b, "type", None) == "tool_use" for b in m.content)
        )
    )
    results_idx = _index(
        lambda m: any(getattr(b, "type", None) == "tool_result" for b in m.content)
    )
    alignment_idx = _index(
        lambda m: (
            m.role == "user" and any("steer north" in getattr(b, "text", "") for b in m.content)
        )
    )
    assert assistant_idx < results_idx < alignment_idx

    # The alignment reached the (single) provider call of the following turn.
    assert provider.requests and "steer north" in _text_messages(provider.requests[0])


async def test_resume_stop_success_closes_pending_tool_bracket_as_success() -> None:
    from linch import HookResult, RunOptions
    from linch.run_store import RunCheckpoint
    from linch.types import Message, TextBlock, ToolResultBlock, ToolUseBlock, Usage

    class StopBeforeProvider:
        resume_policy_id = "test.stop-before-provider"
        resume_policy_config: dict[str, object] = {}

        def on_before_provider_call(self, ctx: Any) -> Any:
            return HookResult.stop("review complete", metadata={"subtype": "success"})

    session_store = _memory_session_store()
    run_store = _memory_run_store()
    agent = _agent(
        _TextProvider(fail_on_call=True),
        _QuickTool(),
        session_store=session_store,
        run_store=run_store,
        hooks=[StopBeforeProvider()],
    )
    session = await agent.session(id="s-stop-success")
    assistant = Message(
        role="assistant",
        content=[ToolUseBlock(id="call-stop", name="A", input={})],
    )
    await session.append([Message(role="user", content=[TextBlock(text="go")]), assistant])
    run = await run_store.create_run(session.id, id="run-stop-success")
    await run_store.save_checkpoint(
        run.id,
        RunCheckpoint(
            phase="tool_batch_pending",
            prompt="go",
            turn_index=0,
            total_usage=Usage(),
            assistant_message=assistant,
            assistant_stop_reason="tool_use",
            pending_tool_blocks=[ToolUseBlock(id="call-stop", name="A", input={})],
        ),
    )

    events = await _collect(session.resume(run.id, RunOptions(allow_legacy_resume=True)))

    assert events[-1].type == "result" and events[-1].subtype == "success"
    result = session.provider_view[-1].content[0]
    assert isinstance(result, ToolResultBlock)
    assert result.tool_use_id == "call-stop"
    assert result.is_error is False


async def test_resume_terminal_turn_drops_restored_alignment_silently() -> None:
    """A resumed turn that finalizes without another provider call drops the
    restored entries silently — no injection, no error (documented limitation)."""
    from linch import RunOptions
    from linch.run_store import RunCheckpoint
    from linch.types import Message, TextBlock, Usage

    session_store = _memory_session_store()
    run_store = _memory_run_store()
    provider = _TextProvider(fail_on_call=True)
    agent = _agent(provider, _QuickTool(), session_store=session_store, run_store=run_store)
    session = await agent.session(id="s1")

    user_msg = Message(role="user", content=[TextBlock(text="start")])
    assistant_msg = Message(role="assistant", content=[TextBlock(text="done")])
    await session.append([user_msg, assistant_msg])

    run = await run_store.create_run("s1", id="run-final")
    await run_store.save_checkpoint(
        run.id,
        RunCheckpoint(
            phase="assistant_appended",
            prompt="start",
            turn_index=0,
            total_usage=Usage(),
            assistant_message=assistant_msg,
            assistant_stop_reason="end_turn",
            pending_alignment=[{"prompt": "steer north", "images": None}],
        ),
    )

    events = await _collect(session.resume(run.id, RunOptions(allow_legacy_resume=True)))

    assert not _alignment_events(events)
    assert not [e for e in events if getattr(e, "type", None) == "error"]
    assert events[-1].type == "result" and events[-1].subtype == "success"
