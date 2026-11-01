"""Tests for the loop guard subsystem (Phase 6).

Unit tests cover evaluate_loop_guard in isolation; integration tests run a
fake provider through run_loop to verify the guard actually stops loops.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from tests.support.agents import make_agent
from tests.support.tools import StubTool, tool_registry

# ---------------------------------------------------------------------------
# Helpers shared by both unit and integration sections
# ---------------------------------------------------------------------------


def _make_tool_block(name: str, input: dict[str, Any] | None = None):
    """Build the attributes consumed by guard evaluation."""

    return SimpleNamespace(name=name, input=dict(input or {}))


def _make_result_block(is_error: bool = False):
    """Build the attributes consumed by guard evaluation."""

    return SimpleNamespace(is_error=is_error)


# ---------------------------------------------------------------------------
# Unit tests — evaluate_loop_guard
# ---------------------------------------------------------------------------


def test_no_trip_on_distinct_calls():
    from linch.loop_guard import LoopGuard, LoopGuardState, evaluate_loop_guard

    guard = LoopGuard(max_identical_tool_calls=3, max_consecutive_failures=3)
    state = LoopGuardState()

    # Three calls with *different* inputs — should not trip
    for i in range(3):
        blocks = [_make_tool_block("Search", {"q": str(i)})]
        results = [_make_result_block(is_error=False)]
        decision = evaluate_loop_guard(guard, state, blocks, results)
        assert decision.action == "continue", f"tripped unexpectedly on turn {i}"


def test_trips_on_identical_calls_at_threshold():
    from linch.loop_guard import LoopGuard, LoopGuardState, evaluate_loop_guard

    guard = LoopGuard(max_identical_tool_calls=3)
    state = LoopGuardState()
    blocks = [_make_tool_block("Search", {"q": "hello"})]
    results = [_make_result_block()]

    # First two calls — continue
    for _ in range(2):
        decision = evaluate_loop_guard(guard, state, blocks, results)
        assert decision.action == "continue"

    # Third call — trip
    decision = evaluate_loop_guard(guard, state, blocks, results)
    assert decision.action == "stop"
    assert decision.reason == "repeated_tool_call"
    assert "Search" in decision.detail


def test_force_final_answer_flag():
    from linch.loop_guard import LoopGuard, LoopGuardState, evaluate_loop_guard

    guard = LoopGuard(max_identical_tool_calls=2, force_final_answer=True)
    state = LoopGuardState()
    blocks = [_make_tool_block("Read", {"path": "/a"})]
    results = [_make_result_block()]

    evaluate_loop_guard(guard, state, blocks, results)  # 1st — continue
    decision = evaluate_loop_guard(guard, state, blocks, results)  # 2nd — trip
    assert decision.action == "force_final"


def test_failure_streak_trips():
    from linch.loop_guard import LoopGuard, LoopGuardState, evaluate_loop_guard

    guard = LoopGuard(max_identical_tool_calls=0, max_consecutive_failures=3)
    state = LoopGuardState()
    blocks = [_make_tool_block("Bash", {"cmd": "ls"})]

    # Two batches of all-errors — continue
    for _ in range(2):
        decision = evaluate_loop_guard(guard, state, blocks, [_make_result_block(is_error=True)])
        assert decision.action == "continue"

    # Third all-error batch — trip
    decision = evaluate_loop_guard(guard, state, blocks, [_make_result_block(is_error=True)])
    assert decision.action == "stop"
    assert decision.reason == "repeated_failures"


def test_success_resets_failure_streak():
    from linch.loop_guard import LoopGuard, LoopGuardState, evaluate_loop_guard

    guard = LoopGuard(max_identical_tool_calls=0, max_consecutive_failures=3)
    state = LoopGuardState()
    blocks = [_make_tool_block("Bash", {"cmd": "ls"})]

    # Two failures → streak = 2
    evaluate_loop_guard(guard, state, blocks, [_make_result_block(is_error=True)])
    evaluate_loop_guard(guard, state, blocks, [_make_result_block(is_error=True)])
    assert state.consecutive_failures == 2

    # One success — resets streak
    evaluate_loop_guard(guard, state, blocks, [_make_result_block(is_error=False)])
    assert state.consecutive_failures == 0

    # Two more failures — should still be < 3 threshold
    evaluate_loop_guard(guard, state, blocks, [_make_result_block(is_error=True)])
    decision = evaluate_loop_guard(guard, state, blocks, [_make_result_block(is_error=True)])
    assert decision.action == "continue", "streak reset should have postponed trip"


def test_disable_identical_check_with_zero():
    from linch.loop_guard import LoopGuard, LoopGuardState, evaluate_loop_guard

    guard = LoopGuard(max_identical_tool_calls=0, max_consecutive_failures=0)
    state = LoopGuardState()
    blocks = [_make_tool_block("Read", {"path": "/a"})]
    results = [_make_result_block()]

    for _ in range(10):
        decision = evaluate_loop_guard(guard, state, blocks, results)
        assert decision.action == "continue"


def test_normalize_loop_guard():
    from linch.loop_guard import LoopGuard, normalize_loop_guard

    assert normalize_loop_guard(None) is None
    assert normalize_loop_guard(False) is None

    lg = normalize_loop_guard(LoopGuard(max_identical_tool_calls=5))
    assert isinstance(lg, LoopGuard)
    assert lg.max_identical_tool_calls == 5

    lg2 = normalize_loop_guard({"max_identical_tool_calls": 7, "force_final_answer": True})
    assert isinstance(lg2, LoopGuard)
    assert lg2.max_identical_tool_calls == 7
    assert lg2.force_final_answer is True


def test_normalize_loop_guard_type_error():
    from linch.loop_guard import normalize_loop_guard

    with pytest.raises(TypeError):
        normalize_loop_guard(42)


def test_guard_on_by_default():
    """Agent construction without loop_guard enables the default guard."""
    from linch.config import FeatureFlags
    from linch.loop_guard import LoopGuard

    agent = make_agent(
        LoopingProvider(),
        tools=tool_registry(StubTool(name="FakeTool", scope="read", parallel=True, output="ok")),
        run_store=None,
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
    )

    assert isinstance(agent.loop_guard, LoopGuard), (
        "Agent() without loop_guard should default to LoopGuard()"
    )


# ---------------------------------------------------------------------------
# Integration tests — loop guard wired into run_loop via a fake provider
# ---------------------------------------------------------------------------


class LoopingProvider:
    """Provider that always responds with the same tool call, looping forever
    unless the guard stops it.  Switches to a text response when it detects
    that no tools are available (force_final turn)."""

    id = "looping"

    def __init__(
        self, tool_name: str = "FakeTool", tool_input: dict[str, Any] | None = None
    ) -> None:
        self.tool_name = tool_name
        self.tool_input = dict(tool_input) if tool_input is not None else {}
        self.call_count = 0

    def context_window(self, model: str) -> int:
        return 128_000

    async def stream(self, req):
        from linch.types import Usage

        self.call_count += 1
        # If tools were stripped by force_final, return a plain text response.
        if not req.tools:
            yield {"type": "message_start", "model": req.model}
            yield {"type": "text_delta", "text": "I cannot proceed further."}
            yield {
                "type": "message_end",
                "stop_reason": "end_turn",
                "usage": Usage(),
                "provider_metadata": None,
            }
            return

        tool_id = f"t{self.call_count}"
        import json

        yield {"type": "message_start", "model": req.model}
        yield {"type": "tool_use_start", "id": tool_id, "name": self.tool_name}
        yield {
            "type": "tool_use_input_delta",
            "id": tool_id,
            "json_delta": json.dumps(self.tool_input),
        }
        yield {"type": "tool_use_end", "id": tool_id}
        yield {
            "type": "message_end",
            "stop_reason": "tool_use",
            "usage": Usage(),
            "provider_metadata": None,
        }


def _make_agent(
    provider: Any,
    *,
    loop_guard: Any = None,
    tool_name: str = "FakeTool",
    **overrides: Any,
):
    """Build a minimal Agent suitable for loop guard integration tests."""
    from linch.config import FeatureFlags

    return make_agent(
        provider,
        tools=tool_registry(StubTool(name=tool_name, scope="read", parallel=True, output="ok")),
        run_store=None,
        loop_guard=loop_guard,
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        **overrides,
    )


async def _collect(session, prompt: str = "go"):
    events = []
    async for event in session.run(prompt):
        events.append(event)
    return events


@pytest.mark.asyncio
async def test_guard_stops_repeating_tool_loop():
    """Guard (max_identical=3) should stop the loop after 3 identical calls."""
    from linch.events import LoopGuardEvent, ResultEvent
    from linch.loop_guard import LoopGuard

    provider = LoopingProvider(tool_name="FakeTool", tool_input={"path": "/a"})
    guard = LoopGuard(max_identical_tool_calls=3, max_consecutive_failures=0)
    agent = _make_agent(provider, loop_guard=guard)
    session = await agent.session()

    events = await _collect(session)

    guard_events = [e for e in events if isinstance(e, LoopGuardEvent)]
    result_events = [e for e in events if isinstance(e, ResultEvent)]

    assert len(guard_events) == 1, "expected exactly one LoopGuardEvent"
    assert guard_events[0].reason == "repeated_tool_call"
    assert guard_events[0].action == "stop"
    assert result_events[-1].subtype == "error"

    # The provider should have been called exactly 3 times (trips on the 3rd)
    assert provider.call_count == 3


@pytest.mark.asyncio
async def test_guard_force_final_answer():
    """force_final_answer=True should inject a reminder and allow one text turn."""
    from linch.events import LoopGuardEvent, ResultEvent
    from linch.loop_guard import LoopGuard

    provider = LoopingProvider(tool_name="FakeTool", tool_input={"x": 1})
    guard = LoopGuard(max_identical_tool_calls=2, force_final_answer=True)
    agent = _make_agent(provider, loop_guard=guard)
    session = await agent.session()

    events = await _collect(session)

    guard_events = [e for e in events if isinstance(e, LoopGuardEvent)]
    result_events = [e for e in events if isinstance(e, ResultEvent)]

    assert len(guard_events) == 1
    assert guard_events[0].action == "force_final"
    # After the force-final turn the provider sees no tools, returns text,
    # and the loop exits successfully.
    assert result_events[-1].subtype == "success"
    assert result_events[-1].final_text is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("force_final", "expected_action", "expected_subtype"),
    [(False, "stop", "error"), (True, "force_final", "success")],
)
async def test_nondurable_guard_still_enforces_stop_or_force_final(
    force_final, expected_action, expected_subtype
):
    from linch.events import LoopGuardEvent, ResultEvent
    from linch.loop_guard import LoopGuard

    provider = LoopingProvider(tool_input={"same": True})
    agent = _make_agent(
        provider,
        loop_guard=LoopGuard(
            max_identical_tool_calls=2,
            max_consecutive_failures=0,
            force_final_answer=force_final,
        ),
    )
    session = await agent.session()
    events = await _collect(session)

    guard_events = [event for event in events if isinstance(event, LoopGuardEvent)]
    result_events = [event for event in events if isinstance(event, ResultEvent)]
    repeated = [event for event in guard_events if event.reason == "repeated_tool_call"]

    assert len(repeated) == 1
    assert repeated[0].action == expected_action
    assert result_events[-1].subtype == expected_subtype


@pytest.mark.asyncio
async def test_guard_disabled_via_none():
    """loop_guard=None disables all guard checks; loop runs until max_turns."""
    from linch.events import LoopGuardEvent

    provider = LoopingProvider(tool_name="FakeTool", tool_input={"k": "v"})
    agent = _make_agent(provider, loop_guard=None, max_turns=5)
    session = await agent.session()
    events = await _collect(session)

    # With guard disabled there should be no loop_guard trip events
    # (only the max_turns LoopGuardEvent at the end)
    guard_events = [e for e in events if isinstance(e, LoopGuardEvent)]
    assert all(g.reason == "max_turns" for g in guard_events), (
        "guard_disabled should only see max_turns event, got: "
        + str([g.reason for g in guard_events])
    )


@pytest.mark.asyncio
async def test_max_turns_emits_loop_guard_event():
    """Exhausting max_turns should emit a LoopGuardEvent(reason='max_turns')."""
    from linch.events import LoopGuardEvent, ResultEvent

    provider = LoopingProvider()
    agent = _make_agent(provider, loop_guard=None, max_turns=2)
    session = await agent.session()
    events = await _collect(session)

    max_turn_guard = [
        e for e in events if isinstance(e, LoopGuardEvent) and e.reason == "max_turns"
    ]
    assert len(max_turn_guard) == 1, "expected one max_turns LoopGuardEvent"
    results = [e for e in events if isinstance(e, ResultEvent)]
    assert results[-1].subtype == "error"


def test_loop_guard_and_permission_keys_preserve_exact_ascii_escaped_bytes() -> None:
    from linch.loop_guard.guard import LoopGuard, LoopGuardState, evaluate_loop_guard
    from linch.permissions.keys import permission_decision_key

    proposed = {"z": [True, None, {"é": "☃"}], "a": 1.25}
    expected = 'Search:{"a":1.25,"z":[true,null,{"\\u00e9":"\\u2603"}]}'
    assert permission_decision_key("Search", proposed).encode() == expected.encode()
    state = LoopGuardState()
    evaluate_loop_guard(LoopGuard(), state, [_make_tool_block("Search", proposed)], [])
    import hashlib

    digest = hashlib.blake2b(expected.encode(), digest_size=8).hexdigest()
    assert state.call_counts == {digest: 1}


def test_loop_guard_checkpoint_persists_digest_keys_without_inputs() -> None:
    from linch.loop.checkpoint import _loop_guard_state_to_dict
    from linch.loop_guard.guard import LoopGuard, LoopGuardState, evaluate_loop_guard
    from linch.run_store import RunCheckpoint, checkpoint_to_dict
    from linch.types import Usage

    block = _make_tool_block("Search", {"query": "sensitive payload"})
    state = LoopGuardState()
    evaluate_loop_guard(LoopGuard(), state, [block], [])
    snapshot = _loop_guard_state_to_dict(state)
    checkpoint = RunCheckpoint("turn_complete", "go", 0, Usage(), loop_guard_state=snapshot)
    encoded = checkpoint_to_dict(checkpoint)

    key = next(iter(snapshot["call_counts"]))
    assert len(key) == 16
    assert key in encoded["loop_guard_state"]["call_counts"]
    assert "sensitive payload" not in str(encoded["loop_guard_state"])


@pytest.mark.parametrize(
    "raw",
    [
        {"call_counts": {"Search:{}": 1}, "consecutive_failures": 0},
        {"call_counts": {"not-a-digest": 1}, "consecutive_failures": 0},
        {"call_counts": {"0123456789abcdef": True}, "consecutive_failures": 0},
    ],
)
def test_loop_guard_checkpoint_rejects_non_digest_state(raw) -> None:
    from linch.errors import ConfigError
    from linch.loop.checkpoint import _loop_guard_state_from_dict

    with pytest.raises(ConfigError):
        _loop_guard_state_from_dict(raw)
