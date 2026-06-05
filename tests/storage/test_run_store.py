from linch.events import ToolCallEndEvent
from linch.run_store import InMemoryRunStore, RunCheckpoint, SqliteRunStore
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

    events = await store.load_events("run-1")
    assert len(events) == 1
    assert events[0].seq == 1
    assert isinstance(events[0].event, ToolCallEndEvent)
    assert events[0].event.tool_result is not None
    assert events[0].event.tool_result.citations[0].id == "c1"

    done = await store.mark_completed("run-1", checkpoint)
    assert done.status == "completed"
    assert done.checkpoint is not None
    assert done.checkpoint.phase == "completed"


async def test_in_memory_run_store_round_trip() -> None:
    await _exercise_store(InMemoryRunStore())


async def test_sqlite_run_store_round_trip(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    try:
        await _exercise_store(store)
    finally:
        await store.close()
