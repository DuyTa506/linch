import errno
from types import SimpleNamespace

import pytest

from linch.events import ToolCallEndEvent
from linch.kernel import Context
from linch.run_store import (
    InMemoryRunStore,
    RunCheckpoint,
    _StoreUnavailableError,
    durable_call,
)
from linch.run_stores.postgres import PostgresRunStore
from linch.tools import Citation, ToolResult
from linch.types import ToolResultBlock, ToolUseBlock, Usage


@pytest.mark.parametrize(
    ("error", "transient"),
    [
        (OSError(errno.ENETUNREACH, "network unreachable"), True),
    ],
)
async def test_durable_call_classifies_os_failures(error, transient):
    await _assert_durable_error(error, transient)


async def _assert_durable_error(error, transient):
    session = SimpleNamespace(
        agent=SimpleNamespace(context=Context(), run_store=InMemoryRunStore()), active_run_id="run"
    )

    async def fail():
        raise error

    with pytest.raises(_StoreUnavailableError if transient else type(error)) as raised:
        await durable_call(session, "write", fail())
    if transient:
        assert raised.value.__cause__ is error
    else:
        assert raised.value is error


async def test_legacy_batch_override_accepts_unleased_and_rejects_leased_calls() -> None:
    class LegacyBatchStore(InMemoryRunStore):
        async def append_event(self, run_id, event):
            return await super().append_event(run_id, event)

    store = LegacyBatchStore()
    await store.create_run("session", id="legacy-batch")
    event = ToolCallEndEvent(
        tool_use_id="call-legacy",
        tool_name="Search",
        result="ok",
        tool_result=ToolResult(content="ok"),
    )
    assert await store.append_events("legacy-batch", [event]) == [1]
    lease = await store.claim("legacy-batch", "owner", 30.0)
    with pytest.raises(TypeError, match="does not support lease"):
        await store.append_events("legacy-batch", [event], lease=lease)
    assert [row.seq for row in await store.load_events("legacy-batch")] == [1]


@pytest.mark.parametrize("kind", ["memory"])
async def test_run_store_round_trip(kind: str, pg_dsn: str) -> None:
    """Persist the checkpoint and event fields used while a turn resumes."""
    store = InMemoryRunStore() if kind == "memory" else PostgresRunStore(pg_dsn)
    checkpoint = RunCheckpoint(
        phase="tool_batch_pending",
        prompt="hello",
        turn_index=2,
        total_usage=Usage(input_tokens=3, output_tokens=4),
        pending_tool_blocks=[ToolUseBlock(id="call-1", name="Search", input={"q": "x"})],
        completed_tool_results={"call-1": ToolResultBlock(tool_use_id="call-1", content="ok")},
        loop_guard_state={"call_counts": {"Search:{}": 1}, "consecutive_failures": 0},
        current_turn_allowed_tools=["Search"],
        truncation_attempts=2,
        truncation_prefix="chunk one",
        pending_truncation_feedback="continue",
        background_workers={"worker-1": {"worker_id": "worker-1", "status": "running"}},
    )
    try:
        created = await store.create_run("session-1", id="run-1", meta={"kind": "test"})
        assert created.id == "run-1"
        assert created.session_id == "session-1"
        assert created.status == "running"
        assert await store.save_checkpoint("run-1", checkpoint) is None
        saved_record = await store.load_run("run-1")
        assert saved_record is not None
        assert saved_record.status == "running"
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
        assert loaded is not None and loaded.checkpoint is not None
        saved = loaded.checkpoint
        assert saved.phase == "tool_batch_pending"
        assert saved.pending_tool_blocks[0].name == "Search"
        assert saved.completed_tool_results["call-1"].content == "ok"
        assert saved.total_usage.output_tokens == 4
        assert saved.loop_guard_state["call_counts"] == {"Search:{}": 1}
        assert saved.current_turn_allowed_tools == ["Search"]
        assert saved.truncation_attempts == 2
        assert saved.truncation_prefix == "chunk one"
        assert saved.pending_truncation_feedback == "continue"
        assert saved.background_workers["worker-1"]["status"] == "running"
        rows = await store.load_events("run-1")
        assert len(rows) == 1
        row = rows[0]
        assert row.seq == 1
        assert isinstance(row.event, ToolCallEndEvent)
        assert row.event.tool_result is not None
        assert row.event.tool_result.citations[0].id == "c1"
        assert row.event.tool_result.summary == "done"
        assert row.event.tool_result.metadata == {"rank": 1}
        assert row.event.tool_result.duration_ms == 7

        checkpoint.permission_decisions = {
            'WriteThing:{"value":"WriteThing"}': {"schema_version": 3, "decision": "allow"},
            'DeleteFile:{"path":"/x"}': {
                "schema_version": 3,
                "decision": "deny",
                "reason": "user denied",
            },
        }
        await store.save_checkpoint("run-1", checkpoint)
        saved = (await store.load_run("run-1")).checkpoint
        assert saved is not None
        write_decision = saved.permission_decisions['WriteThing:{"value":"WriteThing"}']
        delete_decision = saved.permission_decisions['DeleteFile:{"path":"/x"}']
        assert write_decision["decision"] == "allow"
        assert delete_decision["decision"] == "deny"
        assert delete_decision["reason"] == "user denied"
        assert await store.mark_completed("run-1", checkpoint) is None
        completed = await store.load_run("run-1")
        assert completed is not None
        assert completed.status == "completed"
        assert completed.checkpoint is not None
        assert completed.checkpoint.phase == "completed"
    finally:
        await store.close()


@pytest.mark.parametrize("kind", ["memory"])
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
