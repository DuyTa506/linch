from __future__ import annotations

import json

import pytest


def test_journal_checkpoint_envelope_does_not_require_a_receipt_snapshot() -> None:
    from linch.loop.checkpoint import _model_call_runtime_to_extension

    extension: dict[str, object] = {}

    _model_call_runtime_to_extension(extension, active_primary=None)

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
