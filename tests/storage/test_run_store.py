import json

import pytest

from linch.events import ToolCallEndEvent
from linch.run_store import (
    InMemoryRunStore,
    RunCheckpoint,
    _json_safe,
    canonical_json,
)
from linch.run_stores.postgres import PostgresRunStore
from linch.tools import Citation, ToolResult
from linch.types import ToolResultBlock, ToolUseBlock, Usage


async def _exercise_store(store) -> None:
    rec = await store.create_run("session-1", id="run-1", meta={"kind": "test"})
    assert rec.id == "run-1"
    assert rec.session_id == "session-1"
    assert rec.status == "running"

    checkpoint = RunCheckpoint(
        phase="tool_batch_pending",
        prompt="hello",
        turn_index=2,
        total_usage=Usage(input_tokens=3, output_tokens=4),
        pending_tool_blocks=[ToolUseBlock(id="call-1", name="Search", input={"q": "x"})],
        completed_tool_results={
            "call-1": ToolResultBlock(tool_use_id="call-1", content="ok"),
        },
        loop_guard_state={"call_counts": {"Search:{}": 1}, "consecutive_failures": 0},
        current_turn_allowed_tools=["Search"],
        truncation_attempts=2,
        truncation_prefix="chunk one",
        pending_truncation_feedback="continue",
        background_workers={
            "worker-1": {
                "worker_id": "worker-1",
                "display_name": "Researcher",
                "status": "running",
            }
        },
    )
    await store.save_checkpoint("run-1", checkpoint)

    event = ToolCallEndEvent(
        tool_use_id="call-1",
        tool_name="Search",
        result="ok",
        tool_result=ToolResult(
            content="ok",
            summary="done",
            metadata={"rank": 1},
            citations=[Citation(id="c1", source="unit")],
            duration_ms=7,
        ),
    )
    assert await store.append_event("run-1", event) == 1

    loaded = await store.load_run("run-1")
    assert loaded is not None
    assert loaded.checkpoint is not None
    assert loaded.checkpoint.phase == "tool_batch_pending"
    assert loaded.checkpoint.pending_tool_blocks[0].name == "Search"
    assert loaded.checkpoint.completed_tool_results["call-1"].content == "ok"
    assert loaded.checkpoint.total_usage.output_tokens == 4
    assert loaded.checkpoint.truncation_attempts == 2
    assert loaded.checkpoint.truncation_prefix == "chunk one"
    assert loaded.checkpoint.pending_truncation_feedback == "continue"
    assert loaded.checkpoint.background_workers["worker-1"]["status"] == "running"

    events = await store.load_events("run-1")
    assert len(events) == 1
    assert events[0].seq == 1
    assert isinstance(events[0].event, ToolCallEndEvent)
    assert events[0].event.tool_result is not None
    assert events[0].event.tool_result.citations[0].id == "c1"

    # permission_decisions round-trip
    checkpoint2 = RunCheckpoint(
        phase="permission_pending",
        prompt="hello",
        turn_index=2,
        total_usage=Usage(input_tokens=3, output_tokens=4),
        permission_decisions={
            'WriteThing:{"value":"WriteThing"}': {
                "schema_version": 3,
                "decision": "allow",
                "reason": None,
                "updated_input": None,
            },
            'DeleteFile:{"path":"/x"}': {
                "schema_version": 3,
                "decision": "deny",
                "reason": "user denied",
                "updated_input": None,
            },
        },
    )
    await store.save_checkpoint("run-1", checkpoint2)
    loaded2 = await store.load_run("run-1")
    assert loaded2 is not None
    assert loaded2.checkpoint is not None
    pd = loaded2.checkpoint.permission_decisions
    assert pd['WriteThing:{"value":"WriteThing"}']["decision"] == "allow"
    assert pd['DeleteFile:{"path":"/x"}']["decision"] == "deny"
    assert pd['DeleteFile:{"path":"/x"}']["reason"] == "user denied"

    done = await store.mark_completed("run-1", checkpoint)
    assert done.status == "completed"
    assert done.checkpoint is not None
    assert done.checkpoint.phase == "completed"


async def test_in_memory_run_store_round_trip() -> None:
    await _exercise_store(InMemoryRunStore())


async def test_postgres_run_store_round_trip(
    pg_dsn,
) -> None:
    store = PostgresRunStore(pg_dsn)
    try:
        await _exercise_store(store)
    finally:
        await store.close()


def test_json_safe_handles_cycles_depth_collisions_and_aliases() -> None:
    recursive: dict[str, object] = {}
    recursive["self"] = recursive

    assert _json_safe(recursive) == {"self": "<recursion>"}
    with pytest.raises(TypeError, match="recursive value"):
        canonical_json(recursive)

    nested: list[object] = []
    cursor = nested
    for _ in range(101):
        child: list[object] = []
        cursor.append(child)
        cursor = child
    permissive = _json_safe(nested)
    assert "<max-depth>" in json.dumps(permissive)
    with pytest.raises(TypeError, match="maximum nesting depth"):
        canonical_json(nested)

    colliding = _json_safe({1: "integer", "1": "string", "1~int": "reserved"})
    assert colliding == {
        "1~int~2": "integer",
        "1": "string",
        "1~int": "reserved",
    }

    shared = {"items": [1, 2]}
    aliases = _json_safe({"left": shared, "right": shared})
    assert aliases == {"left": shared, "right": shared}
    assert aliases["left"] is not aliases["right"]
    assert canonical_json({"left": shared, "right": shared})


@pytest.mark.parametrize("kind", ["memory", "postgres"])
async def test_run_stores_normalize_recursive_metadata(
    kind,
    pg_dsn,
) -> None:
    stores = [InMemoryRunStore() if kind == "memory" else PostgresRunStore(pg_dsn)]
    try:
        for index, store in enumerate(stores):
            meta: dict[str, object] = {"kind": "recursive"}
            meta["self"] = meta

            created = await store.create_run("session-1", id=f"run-{index}", meta=meta)
            loaded = await store.load_run(f"run-{index}")

            assert created.meta == {"kind": "recursive", "self": "<recursion>"}
            assert loaded is not None
            assert loaded.meta == created.meta
            assert loaded.meta is not created.meta
    finally:
        for store in stores:
            await store.close()


@pytest.mark.parametrize(
    "history", ["legacy failure", ["legacy failure"], [{"message": "old"}, 7], {}]
)
@pytest.mark.parametrize("kind", ["memory", "postgres"])
async def test_run_stores_replace_malformed_error_history(kind, pg_dsn, history) -> None:
    stores = [InMemoryRunStore() if kind == "memory" else PostgresRunStore(pg_dsn)]
    try:
        for index, store in enumerate(stores):
            run_id = f"run-{index}"
            await store.create_run(
                "session-1",
                id=run_id,
                meta={"errors": history, "kind": "test"},
            )
            recursive_error: dict[str, object] = {"message": "new failure"}
            recursive_error["self"] = recursive_error

            failed, _ = await store.mark_failed(run_id, error=recursive_error)
            loaded = await store.load_run(run_id)

            assert failed.meta["errors"] == [
                {"message": "new failure", "self": "<recursion>"},
            ]
            assert loaded is not None
            assert loaded.meta == failed.meta
            assert loaded.meta is not failed.meta
    finally:
        for store in stores:
            await store.close()


async def test_postgres_checkpoint_results_are_isolated_persisted_snapshots(
    pg_dsn,
) -> None:
    store = PostgresRunStore(pg_dsn)
    try:
        await store.create_run("session-1", id="run-1", meta={"labels": ("a", "b")})
        shared = {"items": [1]}
        recursive: dict[str, object] = {}
        recursive["self"] = recursive
        checkpoint = RunCheckpoint(
            phase="turn_complete",
            prompt="hello",
            turn_index=1,
            total_usage=Usage(input_tokens=1, output_tokens=2),
            extension_state={"example": {"left": shared, "right": shared, "recursive": recursive}},
        )

        saved = await store.save_checkpoint("run-1", checkpoint)
        loaded = await store.load_run("run-1")

        assert saved.checkpoint is not checkpoint
        assert saved.meta == {"labels": ["a", "b"]}
        assert loaded is not None
        assert saved.checkpoint == loaded.checkpoint
        assert saved.meta == loaded.meta
        assert saved.checkpoint is not None
        snapshot = saved.checkpoint.extension_state["example"]
        assert snapshot["left"] == snapshot["right"] == {"items": [1]}
        assert snapshot["left"] is not snapshot["right"]
        assert snapshot["recursive"] == {"self": "<recursion>"}

        shared["items"].append(2)
        assert snapshot["left"] == {"items": [1]}

        error: dict[str, object] = {"message": "boom"}
        error["self"] = error
        failed, _ = await store.mark_failed("run-1", error=error)
        loaded_failed = await store.load_run("run-1")

        assert failed.checkpoint is not None
        assert failed.checkpoint == saved.checkpoint
        assert failed.checkpoint is not saved.checkpoint
        assert failed.meta["errors"] == [{"message": "boom", "self": "<recursion>"}]
        assert loaded_failed is not None
        assert failed.checkpoint == loaded_failed.checkpoint
        assert failed.meta == loaded_failed.meta

        replacement = RunCheckpoint(
            phase="turn_complete",
            prompt="replacement",
            turn_index=2,
            total_usage=Usage(),
        )
        failed_with_checkpoint, _ = await store.mark_failed("run-1", replacement)
        assert replacement.phase == "failed"
        assert failed_with_checkpoint.checkpoint is not replacement
        assert failed_with_checkpoint.checkpoint is not None
        assert failed_with_checkpoint.checkpoint.phase == "failed"
    finally:
        await store.close()


@pytest.mark.parametrize("kind", ["memory", "postgres"])
async def test_run_stores_append_valid_current_error_history(
    kind,
    pg_dsn,
) -> None:
    stores = [InMemoryRunStore() if kind == "memory" else PostgresRunStore(pg_dsn)]
    try:
        for store in stores:
            await store.create_run("session", id="run", meta={"errors": [{"message": "first"}]})
            await store.mark_failed("run", error={"message": "second"})
            record = await store.load_run("run")
            assert record is not None
            assert record.meta["errors"] == [{"message": "first"}, {"message": "second"}]
    finally:
        for store in stores:
            await store.close()
