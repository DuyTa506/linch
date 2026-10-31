from __future__ import annotations

import pytest

import linch
from linch.events import event_from_dict, event_to_dict
from linch.loop.reliability import ReliabilityRuntime
from linch.loop.turn_stopping import TurnStoppingDecision, TurnStoppingDispatcher


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


@pytest.mark.asyncio
async def test_turn_stopping_registration_is_identity_safe_and_serial() -> None:
    dispatcher = TurnStoppingDispatcher()
    calls: list[str] = []

    async def listener(_event: object) -> TurnStoppingDecision:
        calls.append("called")
        return TurnStoppingDecision("continue")

    first = dispatcher.register(listener)
    second = dispatcher.register(listener)
    await first.dispose()
    result = await dispatcher.dispatch(object())  # type: ignore[arg-type]
    assert result.action == "continue"
    assert calls == ["called"]
    await second.dispose()
    assert not dispatcher.active


@pytest.mark.asyncio
async def test_prerequisites_snapshot_and_checkpoint_state() -> None:
    policy = linch.ReliabilityPolicy(
        prerequisites=(linch.ToolPrerequisite("b", ("a",)),),
        completion=linch.CompletionGate(("a",)),
    )
    runtime = ReliabilityRuntime(policy)
    runtime.begin_batch()

    class Tool:
        name = "b"

    class Call:
        tool = Tool()

    assert await runtime.before_permission(Call())
    success_call = type("Call", (), {"tool": type("Tool", (), {"name": "a"})()})()
    await runtime.foreground_result(success_call, None, None)
    assert runtime.missing_completion() == ()
    # The same batch's snapshot remains closed even after the observed result.
    assert await runtime.before_permission(Call())
    state = runtime.checkpoint_state()
    assert state == {"v": 1, "corrections": 0, "succeeded": ["a"]}
    assert ReliabilityRuntime.restore(policy, state).succeeded == {"a"}

