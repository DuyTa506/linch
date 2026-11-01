"""Tool-result recovery requires a cursor and scans only its current batch."""

from __future__ import annotations

import pytest

from linch.events import AssistantEvent, ToolCallEndEvent, ToolCallStartEvent
from linch.loop.checkpoint import _recover_completed_tool_results
from linch.run_store import InMemoryRunStore
from linch.types import Message, TextBlock, ToolUseBlock
from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider
from tests.support.stores import memory_session_store


def _start(tool_use_id: str, name: str = "A") -> ToolCallStartEvent:
    return ToolCallStartEvent(tool_use_id=tool_use_id, tool_name=name, input={}, summary=name)


def _end(tool_use_id: str, result: str, name: str = "A") -> ToolCallEndEvent:
    return ToolCallEndEvent(tool_use_id=tool_use_id, tool_name=name, result=result)


def _assistant(text: str) -> AssistantEvent:
    return AssistantEvent(
        message=Message(role="assistant", content=[TextBlock(text=text)]),
        stop_reason="tool_use",
    )


async def _session_with_store(store: InMemoryRunStore):
    agent = make_agent(
        ScriptedProvider([]),
        model="gpt-5",
        permissions=None,
        session_store=memory_session_store(),
        run_store=store,
        cwd=".",
    )
    return await agent.session(id="s1")


async def test_cursor_bounds_scan_ignoring_prior_turn_same_id() -> None:
    # A tool-use id "call-1" recurs across two turns. Recovering turn 2 (cursor
    # after seq 3) must ignore turn 1's end and re-run the tool, not adopt the
    # stale turn-1 result.
    store = InMemoryRunStore()
    session = await _session_with_store(store)
    await store.create_run("s1", id="r1")
    await store.append_events(
        "r1",
        [
            _assistant("turn1"),  # seq 1
            _start("call-1"),  # seq 2
            _end("call-1", "TURN1_RESULT"),  # seq 3
            _assistant("turn2"),  # seq 4  (turn 2's batch starts after here)
        ],
    )
    blocks = [ToolUseBlock(id="call-1", name="A", input={})]

    recovered = await _recover_completed_tool_results(session, "r1", {}, blocks, after_seq=4)
    # No end after seq 4 → nothing recovered → the tool re-runs.
    assert recovered == {}

    # By contrast a full (unbounded) scan would wrongly adopt the turn-1 result.
    leaky = await _recover_completed_tool_results(session, "r1", {}, blocks, after_seq=0)
    assert leaky["call-1"].content == "TURN1_RESULT"


async def test_started_without_end_is_interrupted_placeholder() -> None:
    store = InMemoryRunStore()
    session = await _session_with_store(store)
    await store.create_run("s1", id="r1")
    await store.append_events("r1", [_assistant("t"), _start("call-1")])
    blocks = [ToolUseBlock(id="call-1", name="A", input={})]
    recovered = await _recover_completed_tool_results(session, "r1", {}, blocks, after_seq=1)
    assert recovered["call-1"].is_error
    assert "interrupted" in recovered["call-1"].content


async def test_end_without_matching_start_in_window_is_ignored() -> None:
    # An end whose start is not in the scanned window is not accepted (start→end
    # ordering guard), so a truncated window cannot fabricate a result.
    store = InMemoryRunStore()
    session = await _session_with_store(store)
    await store.create_run("s1", id="r1")
    await store.append_events("r1", [_start("call-1"), _end("call-1", "R")])
    blocks = [ToolUseBlock(id="call-1", name="A", input={})]
    # Cursor at seq 1 excludes the start (seq 1) but includes the end (seq 2).
    recovered = await _recover_completed_tool_results(session, "r1", {}, blocks, after_seq=1)
    assert recovered == {}


@pytest.mark.parametrize("cursor", [None, -1, True, "1"])
async def test_recovery_requires_valid_cursor(cursor) -> None:
    store = InMemoryRunStore()
    session = await _session_with_store(store)
    await store.create_run("s1", id="r1")
    await store.append_events("r1", [_assistant("turn"), _start("call-1"), _end("call-1", "R")])
    blocks = [ToolUseBlock(id="call-1", name="A", input={})]
    with pytest.raises(ValueError, match="tool.*cursor"):
        await _recover_completed_tool_results(session, "r1", {}, blocks, after_seq=cursor)
