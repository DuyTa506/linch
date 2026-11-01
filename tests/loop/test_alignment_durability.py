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
from typing import Any

import pytest

from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn, ToolUseTurn
from tests.support.runs import pin_run_contract
from tests.support.stores import memory_run_store, memory_session_store
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


def _quick_tool() -> StubTool:
    return StubTool(name="A", output="A-ok")


def _agent(
    provider: Any,
    tool: Any,
    *,
    session_store: Any,
    run_store: Any,
    hooks: Any = None,
) -> Any:
    from linch.config import FeatureFlags

    return make_agent(
        provider,
        tools=tool_registry(tool),
        permissions={"mode": "skip-dangerous"},
        session_store=session_store,
        run_store=run_store,
        hooks=hooks,
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        result_offload=None,
        loop_guard=None,
    )


def _memory_session_store():
    return memory_session_store()


def _memory_run_store():
    return memory_run_store()


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


async def test_resume_restores_and_injects_pending_alignment() -> None:
    """A restored queue drains at the next turn boundary, exactly once."""
    from linch.run_store import RunCheckpoint
    from linch.types import Usage

    session_store = _memory_session_store()
    run_store = _memory_run_store()
    provider = ScriptedProvider([TextTurn("done")])
    agent = _agent(provider, _quick_tool(), session_store=session_store, run_store=run_store)
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

    await pin_run_contract(session, run.id)
    events = await _collect(session.resume(run.id))

    assert len(_alignment_events(events)) == 1
    assert provider.requests and "steer north" in _text_messages(provider.requests[0])
    assert events[-1].type == "result" and events[-1].subtype == "success"
    loaded = await run_store.load_run(run.id)
    assert loaded is not None and loaded.checkpoint is not None
    assert loaded.checkpoint.pending_alignment == []


async def test_crash_after_enqueue_before_drain_resume_injects() -> None:
    """Full crash/resume: a second agent on the same stores injects the intent."""
    from linch.errors import ConfigError

    session_store = _memory_session_store()
    run_store = _memory_run_store()
    tool = _wait_tool()
    agent = _agent(
        _tool_then_text_provider(), tool, session_store=session_store, run_store=run_store
    )
    session = await agent.session(id="s1")

    run_id, align_task = await _crash_after_tool_result(session, tool)
    run = await run_store.load_run(run_id)
    assert run is not None and run.checkpoint is not None
    assert run.checkpoint.pending_alignment == [{"prompt": "steer north", "images": None}]
    await session.aclose(force=True)
    with pytest.raises(ConfigError):
        await align_task

    provider2 = ScriptedProvider([TextTurn("done")])
    agent2 = _agent(provider2, _wait_tool(), session_store=session_store, run_store=run_store)
    resumed = await agent2.session(id="s1")
    events = await _collect(resumed.resume(run_id))

    assert len(_alignment_events(events)) == 1
    assert provider2.requests and "steer north" in _text_messages(provider2.requests[0])
    assert events[-1].type == "result" and events[-1].subtype == "success"


async def test_resume_mid_tool_batch_defers_alignment_after_tool_results() -> None:
    """A mid-turn resume never injects between assistant(tool_use) and tool results."""
    from linch.run_store import RunCheckpoint
    from linch.types import Message, TextBlock, ToolUseBlock, Usage

    session_store = _memory_session_store()
    run_store = _memory_run_store()
    provider = ScriptedProvider([TextTurn("done")])
    agent = _agent(provider, _quick_tool(), session_store=session_store, run_store=run_store)
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
            tool_batch_event_after_seq=0,
            prompt="start",
            turn_index=0,
            total_usage=Usage(),
            assistant_message=assistant_msg,
            pending_tool_blocks=[ToolUseBlock(id="call-1", name="A", input={})],
            pending_alignment=[{"prompt": "steer north", "images": None}],
        ),
    )

    await pin_run_contract(session, run.id)
    events = await _collect(session.resume(run.id))

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
    from linch import HookResult
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
        ScriptedProvider([]),
        _quick_tool(),
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
            tool_batch_event_after_seq=0,
            prompt="go",
            turn_index=0,
            total_usage=Usage(),
            assistant_message=assistant,
            assistant_stop_reason="tool_use",
            pending_tool_blocks=[ToolUseBlock(id="call-stop", name="A", input={})],
        ),
    )

    await pin_run_contract(session, run.id)
    events = await _collect(session.resume(run.id))

    assert events[-1].type == "result" and events[-1].subtype == "success"
    result = session.provider_view[-1].content[0]
    assert isinstance(result, ToolResultBlock)
    assert result.tool_use_id == "call-stop"
    assert result.is_error is False


async def test_resume_terminal_turn_drops_restored_alignment_silently() -> None:
    """A resumed turn that finalizes without another provider call drops the
    restored entries silently — no injection, no error (documented limitation)."""
    from linch.run_store import RunCheckpoint
    from linch.types import Message, TextBlock, Usage

    session_store = _memory_session_store()
    run_store = _memory_run_store()
    provider = ScriptedProvider([])
    agent = _agent(provider, _quick_tool(), session_store=session_store, run_store=run_store)
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

    await pin_run_contract(session, run.id)
    events = await _collect(session.resume(run.id))

    assert not _alignment_events(events)
    assert not [e for e in events if getattr(e, "type", None) == "error"]
    assert events[-1].type == "result" and events[-1].subtype == "success"
