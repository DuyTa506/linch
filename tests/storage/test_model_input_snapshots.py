from __future__ import annotations

import sqlite3
from dataclasses import FrozenInstanceError, replace

import pytest

from linch.run_store import (
    MODEL_INPUT_CODEC_VERSION,
    InMemoryRunStore,
    ModelInputSnapshotError,
    RunCheckpoint,
    SqliteRunStore,
    checkpoint_from_dict,
    checkpoint_to_dict,
    create_model_input_snapshot,
    decode_model_input_snapshot,
)
from linch.types import (
    ImageBlock,
    Message,
    OutputSchema,
    ProviderRequest,
    RedactedThinkingBlock,
    SystemBlock,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)


def _request(*, signal: object | None = None) -> ProviderRequest:
    return ProviderRequest(
        model="provider/model-exact",
        system=[
            SystemBlock(text="static", cacheable=True),
            SystemBlock(text="dynamic \N{SNOWMAN}", cacheable=False),
        ],
        tools=[
            {
                "name": "Search",
                "description": "Search",
                "input_schema": {
                    "type": "object",
                    "properties": {"q": {"type": "string"}},
                },
            }
        ],
        messages=[
            Message(
                role="user",
                content=[
                    TextBlock(text="find it"),
                    ImageBlock(source={"type": "url", "url": "https://example.test/a.png"}),
                ],
                provider_metadata={"cache": {"hit": False}},
            ),
            Message(
                role="assistant",
                content=[
                    ThinkingBlock(thinking="reason", signature="sig"),
                    RedactedThinkingBlock(data="opaque"),
                    ToolUseBlock(id="call-1", name="Search", input={"q": "linch"}),
                ],
            ),
            Message(
                role="user",
                content=[
                    ToolResultBlock(
                        tool_use_id="call-1",
                        content=[TextBlock(text="answer"), ImageBlock(source={"url": "ref"})],
                    )
                ],
            ),
        ],
        signal=signal,
        max_output_tokens=1234,
        temperature=0.25,
        stop_sequences=["STOP"],
        max_retries=7,
        reasoning={"effort": "high", "summary": "auto"},
        cache_prompt=True,
        cache_ttl="1h",
        thinking={"type": "enabled", "budget_tokens": 1024},
        effort="xhigh",
        output_schema=OutputSchema(
            name="Answer",
            schema={"type": "object", "required": ["answer"]},
            strict=True,
            description="Final answer",
        ),
        tool_choice={"type": "tool", "name": "Search"},
        stream_partials=False,
    )


def test_snapshot_codec_round_trips_every_request_field_and_rebinds_signal() -> None:
    original_signal = object()
    rebound_signal = object()
    request = _request(signal=original_signal)

    snapshot = create_model_input_snapshot(
        "run-1",
        3,
        request,
        id="snapshot-1",
        created_at="2026-08-13T00:00:00+00:00",
    )
    restored = decode_model_input_snapshot(snapshot, signal=rebound_signal)

    assert snapshot.codec_version == MODEL_INPUT_CODEC_VERSION
    assert snapshot.request_json == snapshot.request_json.encode().decode()
    assert "signal" not in snapshot.request_json
    assert restored == replace(request, signal=rebound_signal)
    assert restored.signal is rebound_signal
    with pytest.raises(FrozenInstanceError):
        snapshot.id = "mutated"  # type: ignore[misc]


def test_snapshot_codec_rejects_corruption_and_incompatible_version() -> None:
    snapshot = create_model_input_snapshot("run-1", 1, _request())

    with pytest.raises(ModelInputSnapshotError, match="integrity"):
        decode_model_input_snapshot(replace(snapshot, request_json="{}"))
    with pytest.raises(ModelInputSnapshotError, match="codec version"):
        decode_model_input_snapshot(replace(snapshot, codec_version=999))


@pytest.mark.parametrize(
    "bad_value, message",
    [
        (float("nan"), "non-finite"),
        ({1: "not-json"}, "mapping key must be str"),
        (object(), "non-JSON value"),
    ],
)
def test_snapshot_codec_strictly_rejects_non_json_request_data(
    bad_value: object,
    message: str,
) -> None:
    request = _request(signal=object())
    request.tools[0]["bad"] = bad_value

    with pytest.raises(TypeError, match=message):
        create_model_input_snapshot("run-1", 1, request)


def test_snapshot_codec_rejects_cycles_but_never_serializes_signal() -> None:
    request = _request(signal=object())
    recursive: dict[str, object] = {}
    recursive["self"] = recursive
    request.reasoning = recursive

    with pytest.raises(TypeError, match="recursive"):
        create_model_input_snapshot("run-1", 1, request)

    request.reasoning = None
    create_model_input_snapshot("run-1", 1, request)


def test_checkpoint_snapshot_fields_are_additive_and_round_trip() -> None:
    legacy = RunCheckpoint(
        phase="provider_pending",
        prompt="hello",
        turn_index=1,
        total_usage=Usage(),
    )
    legacy_wire = checkpoint_to_dict(legacy)
    assert "provider_attempt" not in legacy_wire
    assert "model_input_snapshot_id" not in legacy_wire

    exact = replace(
        legacy,
        provider_attempt=2,
        model_input_snapshot_id="snapshot-2",
    )
    wire = checkpoint_to_dict(exact)
    restored = checkpoint_from_dict(wire)

    assert wire["provider_attempt"] == 2
    assert wire["model_input_snapshot_id"] == "snapshot-2"
    assert restored.provider_attempt == 2
    assert restored.model_input_snapshot_id == "snapshot-2"


async def _exercise_snapshot_store(store: InMemoryRunStore | SqliteRunStore) -> None:
    await store.create_run("session-1", id="run-1")
    first = await store.save("run-1", 1, _request(signal=object()))
    second = await store.save("run-1", 2, _request(signal=object()))

    loaded = await store.load(first.id)
    assert loaded == first
    assert loaded is not None
    assert decode_model_input_snapshot(loaded).signal is None

    assert await store.prune("run-1", keep_ids=(second.id,)) == 1
    assert await store.load(first.id) is None
    assert await store.load(second.id) == second
    await store.delete(second.id)
    assert await store.load(second.id) is None

    with pytest.raises(KeyError, match="run not found"):
        await store.save("missing", 1, _request())


async def test_in_memory_model_input_snapshot_store() -> None:
    await _exercise_snapshot_store(InMemoryRunStore())


async def test_sqlite_model_input_snapshot_store_persists_across_restart(tmp_path) -> None:
    path = tmp_path / "runs.db"
    store = SqliteRunStore(path)
    await store.create_run("session-1", id="run-persist")
    snapshot = await store.save("run-persist", 4, _request(signal=object()))
    await store.close()

    reopened = SqliteRunStore(path)
    try:
        loaded = await reopened.load(snapshot.id)
        assert loaded == snapshot
        assert loaded is not None
        assert decode_model_input_snapshot(loaded) == replace(_request(), signal=None)
    finally:
        await reopened.close()


async def test_sqlite_model_input_snapshot_store_contract(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "contract.db")
    try:
        await _exercise_snapshot_store(store)
    finally:
        await store.close()


async def test_sqlite_load_fails_closed_for_corrupt_snapshot(tmp_path) -> None:
    path = tmp_path / "corrupt.db"
    store = SqliteRunStore(path)
    await store.create_run("session-1", id="run-1")
    snapshot = await store.save("run-1", 1, _request())
    await store.close()

    conn = sqlite3.connect(path)
    conn.execute(
        "update model_input_snapshots set request_json = '{}' where id = ?",
        (snapshot.id,),
    )
    conn.commit()
    conn.close()

    reopened = SqliteRunStore(path)
    try:
        with pytest.raises(ModelInputSnapshotError, match="integrity"):
            await reopened.load(snapshot.id)
    finally:
        await reopened.close()


async def test_sqlite_schema_migrates_a_legacy_database_additively(tmp_path) -> None:
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        create table runs (
          id text primary key,
          session_id text not null,
          status text not null,
          created_at text not null,
          updated_at text not null,
          checkpoint text,
          meta text not null
        );
        create table run_events (
          run_id text not null,
          seq integer not null,
          appended_at text not null,
          event text not null,
          primary key (run_id, seq)
        );
        insert into runs values (
          'legacy-run', 'legacy-session', 'running', 'before', 'before', null, '{}'
        );
        """
    )
    conn.commit()
    conn.close()

    store = SqliteRunStore(path)
    try:
        legacy = await store.load_run("legacy-run")
        snapshot = await store.save("legacy-run", 1, _request())
        assert legacy is not None
        assert legacy.checkpoint is None
        assert await store.load(snapshot.id) == snapshot
    finally:
        await store.close()
