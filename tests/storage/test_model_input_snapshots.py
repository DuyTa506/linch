from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from linch._wire import UnsupportedWireVersionError
from linch.run_store import (
    MODEL_INPUT_CODEC_VERSION,
    InMemoryRunStore,
    ModelInputSnapshotError,
    RunCheckpoint,
    checkpoint_from_dict,
    checkpoint_to_dict,
    create_model_input_snapshot,
    decode_model_input_snapshot,
)
from linch.run_stores.postgres import PostgresRunStore
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
    with pytest.raises(UnsupportedWireVersionError, match="start a new run"):
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


def test_checkpoint_snapshot_fields_are_always_written_and_round_trip() -> None:
    legacy = RunCheckpoint(
        phase="provider_pending",
        prompt="hello",
        turn_index=1,
        total_usage=Usage(),
    )
    legacy_wire = checkpoint_to_dict(legacy)
    assert legacy_wire["provider_attempt"] == 0
    assert legacy_wire["model_input_snapshot_id"] is None

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


async def _exercise_snapshot_store(store: InMemoryRunStore | PostgresRunStore) -> None:
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


async def test_postgres_model_input_snapshot_store_persists_across_restart(
    pg_dsn,
) -> None:
    store = PostgresRunStore(pg_dsn)
    await store.create_run("session-1", id="run-persist")
    snapshot = await store.save("run-persist", 4, _request(signal=object()))
    await store.close()

    reopened = PostgresRunStore(pg_dsn)
    try:
        loaded = await reopened.load(snapshot.id)
        assert loaded == snapshot
        assert loaded is not None
        assert decode_model_input_snapshot(loaded) == replace(_request(), signal=None)
    finally:
        await reopened.close()


async def test_postgres_model_input_snapshot_store_contract(
    pg_dsn,
) -> None:
    store = PostgresRunStore(pg_dsn)
    try:
        await _exercise_snapshot_store(store)
    finally:
        await store.close()


async def test_postgres_load_fails_closed_for_corrupt_snapshot(
    pg_dsn,
) -> None:
    store = PostgresRunStore(pg_dsn)
    await store.create_run("session-1", id="run-1")
    snapshot = await store.save("run-1", 1, _request())
    await store.close()

    import asyncpg

    conn = await asyncpg.connect(pg_dsn)
    try:
        await conn.execute(
            "UPDATE model_input_snapshots SET request_json = '{}' WHERE id = $1", snapshot.id
        )
    finally:
        await conn.close()

    reopened = PostgresRunStore(pg_dsn)
    try:
        with pytest.raises(ModelInputSnapshotError, match="integrity"):
            await reopened.load(snapshot.id)
    finally:
        await reopened.close()


def test_snapshot_codec_preserves_golden_bytes_and_integrity_hash() -> None:
    import hashlib

    snapshot = create_model_input_snapshot("run-1", 3, _request())
    assert hashlib.sha256(snapshot.request_json.encode()).hexdigest() == (
        "afcfb1aabbe427c33c50f4772e51c30c14c58a91e5d3d05439a34a752644b0a8"
    )
    assert snapshot.integrity_hash == (
        "sha256:779cf554f6f848fa02f28ae7425e0fc4ec3628c5c9bf9257f3af89d437000111"
    )
