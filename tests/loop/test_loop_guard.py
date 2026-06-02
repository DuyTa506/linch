"""Tests for the loop guard subsystem (Phase 6).

Unit tests cover evaluate_loop_guard in isolation; integration tests run a
fake provider through run_loop to verify the guard actually stops loops.
"""

from __future__ import annotations

from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Helpers shared by both unit and integration sections
# ---------------------------------------------------------------------------


def _make_tool_block(name: str, input: dict[str, Any] | None = None):
    """Minimal duck-typed ToolUseBlock for guard evaluation tests."""

    class _Block:
        pass

    b = _Block()
    b.name = name  # type: ignore[attr-defined]
    b.input = dict(input) if input is not None else {}  # type: ignore[attr-defined]
    return b


def _make_result_block(is_error: bool = False):
    """Minimal duck-typed ToolResultBlock for guard evaluation tests."""

    class _Block:
        pass

    b = _Block()
    b.is_error = is_error  # type: ignore[attr-defined]
    return b


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


def _make_agent(provider: Any, *, loop_guard: Any = None, tool_name: str = "FakeTool"):
    """Build a minimal Agent suitable for loop guard integration tests."""
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.sessions import InMemorySessionStore
    from linch.tools import ToolResult
    from linch.tools.registry import empty_tools

    class _DummyTool:
        description = "Dummy tool"
        input_schema = {"type": "object", "properties": {}}
        scope = "read"
        parallel = True

        def __init__(self, name: str) -> None:
            self.name = name
            self.tags: tuple[str, ...] = ()

        def validate(self, raw):
            return raw

        def summarize(self, input):
            return self.name

        def resources(self, input):
            return []

        async def execute(self, input, ctx):
            return ToolResult(content="ok")

    return Agent(
        model="test-model",
        provider=provider,
        tools=empty_tools(_DummyTool(tool_name)),
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        loop_guard=loop_guard,
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
async def test_guard_disabled_via_none():
    """loop_guard=None disables all guard checks; loop runs until max_turns."""
    from linch.events import LoopGuardEvent

    provider = LoopingProvider(tool_name="FakeTool", tool_input={"k": "v"})
    # Use max_turns=5 so the test terminates, but loop_guard=None
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.sessions import InMemorySessionStore
    from linch.tools import ToolResult
    from linch.tools.registry import empty_tools

    class _T:
        name = "FakeTool"
        description = "t"
        input_schema = {"type": "object", "properties": {}}
        scope = "read"
        parallel = True
        tags: tuple = ()

        def validate(self, r):
            return r

        def summarize(self, i):
            return "t"

        def resources(self, i):
            return []

        async def execute(self, i, c):
            return ToolResult(content="ok")

    agent = Agent(
        model="test-model",
        provider=provider,
        tools=empty_tools(_T()),
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        loop_guard=None,
        max_turns=5,
    )
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
async def test_guard_on_by_default():
    """Agent() without loop_guard argument should default to LoopGuard()."""
    from linch.loop_guard import LoopGuard

    provider = LoopingProvider()

    # _make_agent passes loop_guard=None in the signature default which means
    # "disabled"; here we omit it to check the default Agent() behavior.
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools

    class _T:
        name = "FakeTool"
        description = "t"
        input_schema = {"type": "object", "properties": {}}
        scope = "read"
        parallel = True
        tags: tuple = ()

        def validate(self, r):
            return r

        def summarize(self, i):
            return "t"

        def resources(self, i):
            return []

        async def execute(self, i, c):
            from linch.tools import ToolResult

            return ToolResult(content="ok")

    agent = Agent(
        model="test-model",
        provider=provider,
        tools=empty_tools(_T()),
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        # loop_guard not passed — should default to LoopGuard()
    )
    assert isinstance(agent.loop_guard, LoopGuard), (
        "Agent() without loop_guard should default to LoopGuard()"
    )


@pytest.mark.asyncio
async def test_max_turns_emits_loop_guard_event():
    """Exhausting max_turns should emit a LoopGuardEvent(reason='max_turns')."""
    from linch.events import LoopGuardEvent, ResultEvent

    provider = LoopingProvider()
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.sessions import InMemorySessionStore
    from linch.tools import ToolResult
    from linch.tools.registry import empty_tools

    class _T:
        name = "FakeTool"
        description = "t"
        input_schema = {"type": "object", "properties": {}}
        scope = "read"
        parallel = True
        tags: tuple = ()

        def validate(self, r):
            return r

        def summarize(self, i):
            return "t"

        def resources(self, i):
            return []

        async def execute(self, i, c):
            return ToolResult(content="ok")

    agent = Agent(
        model="test-model",
        provider=provider,
        tools=empty_tools(_T()),
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        loop_guard=None,  # disable guard so we hit max_turns
        max_turns=2,
    )
    session = await agent.session()
    events = await _collect(session)

    max_turn_guard = [
        e for e in events if isinstance(e, LoopGuardEvent) and e.reason == "max_turns"
    ]
    assert len(max_turn_guard) == 1, "expected one max_turns LoopGuardEvent"
    results = [e for e in events if isinstance(e, ResultEvent)]
    assert results[-1].subtype == "error"
