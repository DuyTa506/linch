"""Versioned serialization / resume forward-compat (ROADMAP Phase 5.3).

The `RunCheckpoint` wire format and the stored-event log are a stable, versioned
contract. A checkpoint dict carries an explicit `schema_version`; a loader from an
older binary must tolerate a *newer* checkpoint (unknown future keys, a higher
version) without crashing, and `load_events` must skip an event it cannot decode
(a future event type) rather than aborting the whole resume.

Verify: the version stamp is present; `checkpoint_from_dict` round-trips a
future-versioned dict; `load_events` drops an undecodable row and keeps the rest.
"""

from __future__ import annotations

import json

from linch.events import ToolCallStartEvent
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


async def test_load_events_skips_undecodable_future_events(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    try:
        await store.create_run("session-1", id="run-1")
        await store.append_event(
            "run-1",
            ToolCallStartEvent(tool_use_id="t1", tool_name="Search", input={}, summary="s"),
        )
        # Inject a row from a hypothetical newer schema: an event type this binary
        # does not know how to decode. It must be skipped, not crash the resume.
        future_event = json.dumps({"type": "telepathy_event", "payload": 42})
        await store._exec.run(
            lambda c: c.execute(
                "insert into run_events (run_id, seq, appended_at, event) values (?, ?, ?, ?)",
                ("run-1", 2, "2026-01-01T00:00:00Z", future_event),
            )
        )

        events = await store.load_events("run-1")

        # Only the decodable event survives; the future one is dropped.
        assert len(events) == 1
        assert isinstance(events[0].event, ToolCallStartEvent)
        assert events[0].seq == 1
    finally:
        await store.close()
