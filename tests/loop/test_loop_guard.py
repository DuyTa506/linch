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


def test_disable_identical_check_with_zero():
    from linch.loop_guard import LoopGuard, LoopGuardState, evaluate_loop_guard

    guard = LoopGuard(max_identical_tool_calls=0, max_consecutive_failures=0)
    state = LoopGuardState()
    blocks = [_make_tool_block("Read", {"path": "/a"})]
    results = [_make_result_block()]

    for _ in range(10):
        decision = evaluate_loop_guard(guard, state, blocks, results)
        assert decision.action == "continue"


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
