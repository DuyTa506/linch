from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn, ToolUseTurn
from tests.support.tools import StubTool, tool_registry


def _tool_then_text_provider() -> ScriptedProvider:
    return ScriptedProvider([ToolUseTurn("Wait", {}), TextTurn("done")])


def _wait_tool() -> Any:
    started = asyncio.Event()
    release = asyncio.Event()

    async def wait(_input: dict[str, Any], _ctx: Any) -> None:
        started.set()
        await release.wait()

    tool: Any = StubTool(name="Wait", output="waited", on_execute=wait)
    tool.started = started
    tool.release = release
    return tool


def _agent(provider: Any, tool: Any, *, hooks: Any = None) -> Any:
    from linch.config import FeatureFlags

    return make_agent(
        provider,
        tools=tool_registry(tool),
        permissions={"mode": "skip-dangerous"},
        run_store=None,
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

    provider = _tool_then_text_provider()
    tool = _wait_tool()
    session = await _agent(provider, tool).session()

    with pytest.raises(ConfigError):
        await session.align("not running")


@pytest.mark.asyncio
async def test_interrupt_rejects_pending_alignment() -> None:
    from linch.errors import ConfigError

    provider = _tool_then_text_provider()
    tool = _wait_tool()
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

    provider = _tool_then_text_provider()
    tool = _wait_tool()
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


@pytest.mark.asyncio
async def test_timed_out_alignment_is_not_applied_after_slow_hook() -> None:
    """A deadline during alignment hooks must prevent the queued message."""
    from linch.errors import ConfigError

    hook_entered = asyncio.Event()
    release_hook = asyncio.Event()

    class SlowAlignmentHook:
        async def on_user_prompt_submit(self, ctx: Any) -> None:
            if ctx.source == "align":
                hook_entered.set()
                await release_hook.wait()

    provider = _tool_then_text_provider()
    tool = _wait_tool()
    session = await _agent(provider, tool, hooks=[SlowAlignmentHook()]).session()

    async def collect() -> list[Any]:
        return [event async for event in session.run("start")]

    run_task = asyncio.create_task(collect())
    await tool.started.wait()
    align_task = asyncio.create_task(session.align("change", timeout_s=0.05))
    tool.release.set()
    await hook_entered.wait()
    with pytest.raises(ConfigError, match="timed out before it was applied"):
        await align_task
    release_hook.set()
    events = await run_task

    assert not any(getattr(event, "subtype", None) == "alignment" for event in events)
    assert "change" not in _text_messages(provider.requests[1])


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

    provider = _tool_then_text_provider()
    tool = _wait_tool()
    session = await _agent(provider, tool).session()
    session._active = True  # simulate an active run that never reaches a boundary
    try:
        with pytest.raises(ConfigError, match="timed out"):
            await session.align("change", timeout_s=0.05)
        # The timed-out entry is dropped so it cannot inject into a later turn.
        assert session.alignment_queue == []
    finally:
        session._active = False


async def test_alignment_deadline_after_append_starts_waits_for_truthful_completion():
    from linch.sessions import InMemorySessionStore

    append_started, append_release = asyncio.Event(), asyncio.Event()

    class HeldAppendStore(InMemorySessionStore):
        async def append_messages(self, session_id, messages, **kwargs):
            if any("change" in str(message) for message in messages):
                append_started.set()
                await append_release.wait()
            return await super().append_messages(session_id, messages, **kwargs)

    provider, tool = _tool_then_text_provider(), _wait_tool()
    agent = _agent(provider, tool)
    agent.session_store = HeldAppendStore()
    session = await agent.session()

    async def collect():
        return [event async for event in session.run("start")]

    running = asyncio.create_task(collect())
    await tool.started.wait()
    aligning = asyncio.create_task(session.align("change", timeout_s=0.02))
    tool.release.set()
    await asyncio.wait_for(append_started.wait(), 1)
    await asyncio.sleep(0.04)
    assert not aligning.done()
    append_release.set()
    await aligning
    events = await running
    assert sum(getattr(event, "subtype", None) == "alignment" for event in events) == 1
    assert "change" in _text_messages(provider.requests[1])
    await agent.close()
