"""Versioned serialization / resume wire contracts.

The `RunCheckpoint` wire format and the stored-event log are a strict, versioned
contract. Pre-3.0 and unknown-generation state is rejected. Stored events are
required on read unless a future event explicitly carries a valid
``ignorable: true`` envelope.

Verify: the version stamp is present; `checkpoint_from_dict` rejects an
incompatible-versioned dict; `load_events` preserves explicitly ignorable
events and fails closed for required or malformed rows.
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
from linch.sessions.store import ProviderViewSnapshot, snapshot_from_dict, snapshot_to_dict
from linch.types import Message, ToolResultBlock, ToolUseBlock, Usage


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


def test_provider_snapshot_preserves_identity_and_messages() -> None:
    snapshot = ProviderViewSnapshot(
        provider_view=[
            Message(
                role="user",
                content=[
                    ToolResultBlock(
                        tool_use_id="search-call",
                        content="activated",
                    ),
                ],
            )
        ],
        covers_seq=3,
        projection_id="persisted-projection",
    )

    restored = snapshot_from_dict(snapshot_to_dict(snapshot))

    assert restored.projection_id == "persisted-projection"
    result = restored.provider_view[0].content[0]
    assert isinstance(result, ToolResultBlock)
    assert result.content == "activated"


def test_checkpoint_from_dict_rejects_future_version_before_field_defaults() -> None:
    from linch._wire import UnsupportedWireVersionError

    data = checkpoint_to_dict(_checkpoint())
    # Simulate a checkpoint written by a NEWER binary: bumped version + new field.
    data["schema_version"] = SCHEMA_VERSION + 99
    data["some_future_field"] = {"nested": True}

    with pytest.raises(UnsupportedWireVersionError, match="start a new run"):
        checkpoint_from_dict(data)


@pytest.mark.parametrize(
    "field",
    [
        "phase",
        "prompt",
        "turn_index",
        "total_usage",
        "assistant_message",
        "pending_tool_blocks",
        "completed_tool_results",
        "force_final_pending",
        "loop_guard_state",
        "pending_skill_overlay",
        "current_turn_allowed_tools",
        "assistant_stop_reason",
        "permission_decisions",
        "background_workers",
        "truncation_attempts",
        "truncation_prefix",
        "pending_truncation_feedback",
        "tool_batch_event_after_seq",
        "pending_alignment",
        "extension_state",
        "provider_attempt",
        "model_input_snapshot_id",
    ],
)
def test_checkpoint_requires_every_current_wire_field(field: str) -> None:
    data = checkpoint_to_dict(_checkpoint())
    data.pop(field, None)
    with pytest.raises(KeyError, match=field):
        checkpoint_from_dict(data)


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


def test_checkpoint_round_trips_extension_state() -> None:
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

    empty = checkpoint_to_dict(_checkpoint())
    assert empty["extension_state"] == {}
    assert empty["provider_attempt"] == 0
    assert empty["model_input_snapshot_id"] is None


def test_checkpoint_normalizes_malformed_pending_alignment() -> None:
    data = checkpoint_to_dict(_checkpoint())
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
        future_raw = {
            "type": "telepathy_event",
            "ignorable": True,
            "schema_version": 97,
            "payload": 42,
        }
        stored_future = {"schema_version": SCHEMA_VERSION, "event": future_raw}
        await store._exec.run(
            lambda c: c.execute(
                "insert into run_events (run_id, seq, appended_at, event) values (?, ?, ?, ?)",
                ("run-1", 2, "2026-01-01T00:00:00Z", json.dumps(stored_future)),
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
        (
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "event": {"type": "telepathy_event", "payload": 42},
                }
            ),
            "unknown event type",
        ),
        (
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "event": {"type": "", "ignorable": True},
                }
            ),
            "non-empty string",
        ),
        (
            json.dumps({"schema_version": SCHEMA_VERSION, "event": {"ignorable": True}}),
            "non-empty string",
        ),
        (json.dumps([]), "state was written by Linch < 3.0"),
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


def test_removed_discovery_guard_is_rejected_as_unknown_content() -> None:
    from linch.types import message_from_dict

    with pytest.raises(ValueError, match="unknown content block type"):
        message_from_dict(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_surface_guard",
                        "required_reader_version": 1,
                        "feature_version": 1,
                    }
                ],
            }
        )


@pytest.mark.parametrize("extra_field", ["tool_activations", "unknown_control"])
def test_tool_result_rejects_unknown_fields_instead_of_discarding_facts(extra_field: str) -> None:
    from linch.types import message_from_dict

    with pytest.raises(ValueError, match="unknown tool_result fields"):
        message_from_dict(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "search-call",
                        "content": "activated",
                        extra_field: [{"schema_version": 1, "name": "deferred_tool"}],
                    }
                ],
            }
        )
