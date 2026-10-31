from __future__ import annotations

import pytest

import linch
from linch.events import event_from_dict, event_to_dict


def test_default_policy_and_opt_out_are_distinct() -> None:
    assert linch.ReliabilityPolicy().max_correction_turns == 2
    assert linch.ReliabilityPolicy().retry_invalid_responses is True
    assert linch.Agent(model="test", reliability=None).reliability is None


def test_policy_validation_rejects_bad_graph_and_terminal_evidence() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        linch.ReliabilityPolicy(
            prerequisites=(linch.ToolPrerequisite("b", ("a", "a")),)
        ).validate(("a", "b"))
    with pytest.raises(ValueError, match="cycle"):
        linch.ReliabilityPolicy(
            prerequisites=(
                linch.ToolPrerequisite("a", ("b",)),
                linch.ToolPrerequisite("b", ("a",)),
            )
        ).validate(("a", "b"))
    with pytest.raises(ValueError, match="terminal"):
        linch.ReliabilityPolicy(
            completion=linch.CompletionGate(("final",)),
        ).validate(("final",), final_tool_name="final")


def test_reliability_event_round_trips_without_provider_payload() -> None:
    event = linch.ReliabilityEvent("completion", "continue", "missing", 3, 1)
    raw = event_to_dict(event)
    assert raw == {
        "type": "reliability",
        "mechanism": "completion",
        "action": "continue",
        "reason": "missing",
        "turn_index": 3,
        "correction_count": 1,
    }
    assert event_from_dict(raw) == event

