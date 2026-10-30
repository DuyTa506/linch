from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


def _row(seq: int, event: object) -> SimpleNamespace:
    return SimpleNamespace(seq=seq, event=event)


def _delta(
    batch_id: str,
    *,
    turn_index: int = 0,
    signature: str = 'search:{"q":"x"}',
    before: int = 0,
    count: int = 1,
    failures_before: int = 0,
    failures: int = 0,
    action: str = "continue",
    reason: str = "",
    detail: str = "",
) -> object:
    from linch.events import _LoopGuardEvaluationEvent

    return _LoopGuardEvaluationEvent(
        batch_id=batch_id,
        turn_index=turn_index,
        count_deltas={signature: count},
        count_before={signature: before},
        failures_before=failures_before,
        consecutive_failures=failures,
        action=action,
        reason=reason,
        detail=detail,
    )


def test_journal_checkpoint_envelope_does_not_require_a_receipt_snapshot() -> None:
    from linch.loop.checkpoint import _model_call_runtime_to_extension

    extension: dict[str, object] = {}

    _model_call_runtime_to_extension(extension, ledger_snapshot=None, active_primary=None)

    assert extension["linch.model_calls.v1"] == {
        "version": 3,
        "format": "journal-v1",
        "ledger": None,
        "active_primary": None,
        "journal_count": 0,
    }


def test_journal_checkpoint_bytes_stay_flat_as_fifty_turns_accumulate() -> None:
    from linch.loop.checkpoint import _model_call_runtime_to_extension
    from linch.run_store import RunCheckpoint, checkpoint_to_dict
    from linch.types import Usage

    sizes: list[int] = []
    for turn in range(50):
        checkpoint = RunCheckpoint(
            phase="turn_complete",
            prompt="go",
            turn_index=turn,
            total_usage=Usage(input_tokens=turn),
            extension_state={},
        )
        _model_call_runtime_to_extension(
            checkpoint.extension_state,
            ledger_snapshot=None,
            active_primary=None,
            journal_count=turn + 1,
        )
        sizes.append(len(json.dumps(checkpoint_to_dict(checkpoint), sort_keys=True).encode()))

    assert max(sizes) - min(sizes) <= 16
    assert checkpoint.loop_guard_state is None


def test_old_snapshot_runtime_envelope_is_rejected() -> None:
    from linch.errors import ConfigError
    from linch.loop.checkpoint import _model_call_runtime_from_extension

    with pytest.raises(ConfigError, match="runtime envelope"):
        _model_call_runtime_from_extension(
            {
                "linch.model_calls.v1": {
                    "version": 3,
                    "ledger": {},
                    "active_primary": None,
                }
            }
        )


def test_journal_writer_rejects_a_receipt_snapshot() -> None:
    from linch.errors import ConfigError
    from linch.loop.checkpoint import _model_call_runtime_to_extension

    with pytest.raises(ConfigError, match="cannot include a ledger snapshot"):
        _model_call_runtime_to_extension({}, ledger_snapshot={}, active_primary=None)


def test_loop_guard_delta_is_a_private_required_wire_event() -> None:
    from linch.events import _LoopGuardEvaluationEvent, event_from_dict, event_to_dict

    event = _LoopGuardEvaluationEvent(
        batch_id="batch-1",
        turn_index=0,
        count_deltas={'search:{"q":"x"}': 1},
        count_before={'search:{"q":"x"}': 0},
        failures_before=0,
        consecutive_failures=0,
        action="continue",
    )

    encoded = event_to_dict(event)
    assert encoded["type"] == "loop_guard_delta"
    assert event_from_dict(encoded) == event
    with pytest.raises(ValueError, match="known event type"):
        event_from_dict({**encoded, "ignorable": True})


def test_loop_guard_replay_applies_sparse_deltas_without_a_signature_map_snapshot() -> None:
    from linch.loop.checkpoint import _loop_guard_state_from_events
    from linch.loop_guard import LoopGuard

    rows = [_row(5, _delta("batch-1"))]

    state = _loop_guard_state_from_events(
        LoopGuard(max_identical_tool_calls=3),
        rows,
        evaluation_count=1,
        evaluation_seq=5,
        current_batch_id=None,
    )

    assert state.call_counts == {'search:{"q":"x"}': 1}


def test_loop_guard_replay_excludes_journal_ahead_in_flight_batch() -> None:
    from linch.loop.checkpoint import _loop_guard_state_from_events
    from linch.loop_guard import LoopGuard

    rows = [_row(2, _delta("batch-1"))]

    bounded = _loop_guard_state_from_events(
        LoopGuard(),
        rows,
        evaluation_count=0,
        evaluation_seq=1,
        current_batch_id="batch-1",
    )

    assert bounded.call_counts == {}


def test_loop_guard_replay_fails_closed_when_a_committed_delta_is_missing() -> None:
    from linch.errors import ConfigError
    from linch.loop.checkpoint import _loop_guard_state_from_events
    from linch.loop_guard import LoopGuard

    rows: list[object] = []

    with pytest.raises(ConfigError, match="missing committed"):
        _loop_guard_state_from_events(
            LoopGuard(),
            rows,
            evaluation_count=1,
            evaluation_seq=3,
            current_batch_id=None,
        )


def test_loop_guard_replay_rejects_a_conflicting_duplicate_delta() -> None:
    from linch.errors import ConfigError
    from linch.loop.checkpoint import _loop_guard_state_from_events
    from linch.loop_guard import LoopGuard

    rows = [_row(1, _delta("batch-1")), _row(2, _delta("batch-1", count=2))]

    with pytest.raises(ConfigError, match="conflicting"):
        _loop_guard_state_from_events(
            LoopGuard(), rows, evaluation_count=1, evaluation_seq=2, current_batch_id=None
        )


def test_loop_guard_replay_rejects_a_delta_with_wrong_decision() -> None:
    from linch.errors import ConfigError
    from linch.loop.checkpoint import _loop_guard_state_from_events
    from linch.loop_guard import LoopGuard

    rows = [_row(1, _delta("batch-1"))]

    with pytest.raises(ConfigError, match="threshold"):
        _loop_guard_state_from_events(
            LoopGuard(max_identical_tool_calls=1),
            rows,
            evaluation_count=1,
            evaluation_seq=1,
            current_batch_id=None,
        )


def test_loop_guard_replay_allows_an_assistant_only_terminal_intercept() -> None:
    from linch.loop.checkpoint import _loop_guard_state_from_events
    from linch.loop_guard import LoopGuard

    rows: list[object] = []

    state = _loop_guard_state_from_events(
        LoopGuard(),
        rows,
        evaluation_count=0,
        evaluation_seq=0,
        current_batch_id=None,
    )

    assert state.call_counts == {}


def test_loop_guard_runtime_starts_without_fact_containers() -> None:
    from linch.loop.runtime import RunRuntime

    runtime = RunRuntime(
        session=SimpleNamespace(agent=None),
        prompt="",
        opts=None,
        run_id="run",
        run_record=None,
        resume_checkpoint=None,
    )

    assert runtime.guard_evaluation_ids is None
    assert runtime.guard_evaluation_facts is None


def test_loop_guard_runtime_restore_keeps_only_journal_ahead_facts() -> None:
    from linch.loop.checkpoint import _loop_guard_evaluations_from_events

    rows = [_row(1, _delta("batch-1")), _row(2, _delta("batch-2", turn_index=1))]

    tail = _loop_guard_evaluations_from_events(rows, after_seq=1)

    assert set(tail) == {"batch-2"}
