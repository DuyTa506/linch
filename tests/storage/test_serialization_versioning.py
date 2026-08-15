"""Versioned serialization / resume forward-compat (ROADMAP Phase 5.3).

The `RunCheckpoint` wire format and the stored-event log are a stable, versioned
contract. A checkpoint dict carries an explicit `schema_version`; a loader from an
older binary must tolerate a *newer* checkpoint (unknown future keys, a higher
version) without crashing. Stored events are required on read unless a future
event explicitly carries a valid ``ignorable: true`` envelope.

Verify: the version stamp is present; `checkpoint_from_dict` round-trips a
future-versioned dict; `load_events` preserves explicitly ignorable events and
fails closed for required or malformed rows.
"""

from __future__ import annotations

import json

import pytest

from linch.events import IgnorableEvent, ToolCallStartEvent, event_to_dict
from linch.run_store import (
    SCHEMA_VERSION,
    RunCheckpoint,
    SqliteRunStore,
    checkpoint_from_dict,
    checkpoint_to_dict,
)
from linch.types import ToolUseBlock, Usage


def _checkpoint() -> RunCheckpoint:
    return RunCheckpoint(
        phase="tool_batch_pending",
        prompt="hello",
        turn_index=2,
        total_usage=Usage(input_tokens=3, output_tokens=4),
        pending_tool_blocks=[ToolUseBlock(id="call-1", name="Search", input={"q": "x"})],
        truncation_attempts=1,
        truncation_prefix="chunk one",
        pending_truncation_feedback="continue",
    )


def test_checkpoint_dict_carries_schema_version() -> None:
    data = checkpoint_to_dict(_checkpoint())
    assert data["schema_version"] == SCHEMA_VERSION


def test_checkpoint_from_dict_tolerates_future_version_and_unknown_keys() -> None:
    data = checkpoint_to_dict(_checkpoint())
    # Simulate a checkpoint written by a NEWER binary: bumped version + new field.
    data["schema_version"] = SCHEMA_VERSION + 99
    data["some_future_field"] = {"nested": True}

    restored = checkpoint_from_dict(data)

    assert restored.phase == "tool_batch_pending"
    assert restored.turn_index == 2
    assert restored.pending_tool_blocks[0].name == "Search"
    assert restored.total_usage.output_tokens == 4
    assert restored.truncation_attempts == 1
    assert restored.truncation_prefix == "chunk one"
    assert restored.pending_truncation_feedback == "continue"


def test_checkpoint_from_dict_defaults_missing_truncation_attempts() -> None:
    data = checkpoint_to_dict(_checkpoint())
    data.pop("truncation_attempts")
    data.pop("truncation_prefix")
    data.pop("pending_truncation_feedback")

    restored = checkpoint_from_dict(data)

    assert restored.truncation_attempts == 0
    assert restored.truncation_prefix == ""
    assert restored.pending_truncation_feedback is None


def test_checkpoint_round_trips_pending_alignment_with_images() -> None:
    checkpoint = _checkpoint()
    checkpoint.pending_alignment = [
        {"prompt": "steer north", "images": [{"url": "http://x/img.png"}]},
        {"prompt": "then east", "images": [{"media_type": "image/png", "data": "abc"}]},
        {"prompt": "plain", "images": None},
    ]

    data = json.loads(json.dumps(checkpoint_to_dict(checkpoint)))
    restored = checkpoint_from_dict(data)

    assert restored.pending_alignment == checkpoint.pending_alignment


def test_checkpoint_round_trips_extension_state_and_legacy_defaults_empty() -> None:
    checkpoint = _checkpoint()
    checkpoint.extension_state = {
        "example.extension": {
            "cursor": "cursor-123",
            "budget": {"used": 1, "remaining": 1},
            "seen": ["Search"],
        }
    }

    data = json.loads(json.dumps(checkpoint_to_dict(checkpoint)))
    restored = checkpoint_from_dict(data)
    assert restored.extension_state == checkpoint.extension_state

    # A checkpoint persisted by older Linch releases has no extension namespace.
    data.pop("extension_state")
    assert checkpoint_from_dict(data).extension_state == {}

    assert "extension_state" not in checkpoint_to_dict(_checkpoint())


def test_checkpoint_from_dict_defaults_missing_pending_alignment() -> None:
    data = checkpoint_to_dict(_checkpoint())
    data.pop("pending_alignment")
    assert checkpoint_from_dict(data).pending_alignment == []

    # Malformed entries are skipped or normalized, never crash the resume.
    data["pending_alignment"] = [
        "not-a-dict",
        {"prompt": ""},
        {"no_prompt": 1},
        {"prompt": "ok", "images": "bad"},
    ]
    restored = checkpoint_from_dict(data)
    assert restored.pending_alignment == [{"prompt": "ok", "images": None}]


async def test_load_events_preserves_explicitly_ignorable_future_events(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    try:
        await store.create_run("session-1", id="run-1")
        await store.append_event(
            "run-1",
            ToolCallStartEvent(tool_use_id="t1", tool_name="Search", input={}, summary="s"),
        )
        future_raw = {"type": "telepathy_event", "ignorable": True, "payload": 42}
        await store._exec.run(
            lambda c: c.execute(
                "insert into run_events (run_id, seq, appended_at, event) values (?, ?, ?, ?)",
                ("run-1", 2, "2026-01-01T00:00:00Z", json.dumps(future_raw)),
            )
        )

        events = await store.load_events("run-1")

        assert len(events) == 2
        assert isinstance(events[0].event, ToolCallStartEvent)
        assert events[0].seq == 1
        assert isinstance(events[1].event, IgnorableEvent)
        assert events[1].seq == 2
        assert event_to_dict(events[1].event) == future_raw
    finally:
        await store.close()


@pytest.mark.parametrize(
    ("encoded", "error"),
    [
        (json.dumps({"type": "telepathy_event", "payload": 42}), "unknown event type"),
        (json.dumps({"type": "", "ignorable": True}), "non-empty string"),
        (json.dumps({"ignorable": True}), "non-empty string"),
        (json.dumps([]), "event must be an object"),
        ("{", "Expecting property name"),
    ],
)
async def test_load_events_fails_closed_for_required_or_malformed_rows(
    tmp_path, encoded: str, error: str
) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    try:
        await store.create_run("session-1", id="run-1")
        await store._exec.run(
            lambda c: c.execute(
                "insert into run_events (run_id, seq, appended_at, event) values (?, ?, ?, ?)",
                ("run-1", 1, "2026-01-01T00:00:00Z", encoded),
            )
        )

        with pytest.raises(ValueError, match=error):
            await store.load_events("run-1")
    finally:
        await store.close()
