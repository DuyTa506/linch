import errno
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from linch.events import ToolCallEndEvent
from linch.kernel import Context
from linch.run_store import (
    InMemoryRunStore,
    RunCheckpoint,
    _json_safe,
    _StoreUnavailableError,
    canonical_json,
    durable_call,
)
from linch.run_stores.postgres import PostgresRunStore
from linch.tools import Citation, ToolResult
from linch.types import ToolResultBlock, ToolUseBlock, Usage


@pytest.mark.parametrize(
    ("error", "transient"),
    [
        (PermissionError("certificate denied"), False),
        (FileNotFoundError("certificate missing"), False),
        (OSError("permanent failure"), False),
        (OSError(errno.EIO, "device failure"), False),
        (OSError(errno.ENETUNREACH, "network unreachable"), True),
        (OSError(errno.EHOSTUNREACH, "host unreachable"), True),
        (ConnectionError("connection lost"), True),
        (TimeoutError("query timed out"), True),
    ],
)
async def test_durable_call_classifies_os_failures(error, transient):
    await _assert_durable_error(error, transient)


@pytest.mark.parametrize(
    ("name", "message", "transient"),
    [
        ("DataError", "invalid argument", False),
        ("_base.DataError", "invalid client argument", False),
        ("ClientConfigurationError", "invalid SSL configuration", False),
        ("UnsupportedServerFeatureError", "unsupported server", False),
        ("InterfaceError", "another operation is in progress", False),
        ("InterfaceError", "connection is closed", True),
        ("InterfaceError", "pool is closed", True),
        ("InterfaceError", "pool is closing", True),
        ("PostgresConnectionError", "connection lost", True),
        ("AdminShutdownError", "server shutting down", True),
        ("CrashShutdownError", "server crashed", True),
        ("CannotConnectNowError", "server starting up", True),
        ("TooManyConnectionsError", "server full", True),
    ],
)
@pytest.mark.parametrize("wrapped", [False, True])
async def test_durable_call_classifies_postgres_failures(name, message, transient, wrapped):
    asyncpg = pytest.importorskip("asyncpg")
    if name.startswith("_base."):
        error = getattr(asyncpg.exceptions._base, name.removeprefix("_base."))(message)
    else:
        error = getattr(asyncpg, name)(message)
    if wrapped:
        wrapper = RuntimeError("store operation failed")
        wrapper.__cause__ = error
        error = wrapper
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


async def test_in_memory_event_batch_copies_once_and_fences_once():
    import linch.run_store as run_store

    store = run_store.InMemoryRunStore()
    await store.create_run("session", id="batch")
    await store.claim("batch", "owner", 30.0)
    events = [
        ToolCallEndEvent(
            tool_use_id=f"call-{index}",
            tool_name="Search",
            result="ok",
            tool_result=ToolResult(content="ok", metadata={"labels": ["original"]}),
        )
        for index in range(2)
    ]
    assert await store.append_event("batch", events[0]) == 1
    with (
        patch.object(run_store, "_copy_event", wraps=run_store._copy_event) as copy_event,
        patch.object(store, "_check_lease", wraps=store._check_lease) as check_lease,
    ):
        assert await store.append_events("batch", events) == [2, 3]
        assert (copy_event.call_count, check_lease.call_count) == (2, 1)
        assert await store.append_events("missing", []) == []
        assert (copy_event.call_count, check_lease.call_count) == (2, 1)
    events[0].tool_result.metadata["labels"].append("mutated")
    events.clear()
    rows = await store.load_events("batch")
    assert [row.seq for row in rows] == [1, 2, 3]
    assert [row.event.tool_result.metadata["labels"] for row in rows] == [["original"]] * 3


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


@pytest.mark.parametrize("kind", ["memory", "postgres"])
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

            assert await store.mark_failed(run_id, error=recursive_error) == []
            loaded = await store.load_run(run_id)

            assert loaded is not None
            assert loaded.meta["errors"] == [
                {"message": "new failure", "self": "<recursion>"},
            ]
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

        await store.save_checkpoint("run-1", checkpoint)
        saved = await store.load_run("run-1")

        assert saved is not None
        assert saved.checkpoint is not checkpoint
        assert saved.meta == {"labels": ["a", "b"]}
        assert saved.checkpoint is not None
        snapshot = saved.checkpoint.extension_state["example"]
        assert snapshot["left"] == snapshot["right"] == {"items": [1]}
        assert snapshot["left"] is not snapshot["right"]
        assert snapshot["recursive"] == {"self": "<recursion>"}

        shared["items"].append(2)
        assert snapshot["left"] == {"items": [1]}

        error: dict[str, object] = {"message": "boom"}
        error["self"] = error
        await store.mark_failed("run-1", error=error)
        failed = await store.load_run("run-1")

        assert failed is not None
        assert failed.checkpoint is not None
        assert failed.checkpoint == saved.checkpoint
        assert failed.meta["errors"] == [{"message": "boom", "self": "<recursion>"}]

        replacement = RunCheckpoint(
            phase="turn_complete",
            prompt="replacement",
            turn_index=2,
            total_usage=Usage(),
        )
        await store.mark_failed("run-1", replacement)
        failed_with_checkpoint = await store.load_run("run-1")
        assert replacement.phase == "failed"
        assert failed_with_checkpoint is not None
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
