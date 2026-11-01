from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

import pytest

from linch._wire import WIRE_VERSION, UnsupportedWireVersionError
from linch.events import (
    ModelCallEvent,
    ToolCallStartEvent,
    _stored_event_from_dict,
    event_from_dict,
    event_to_dict,
)
from linch.model_calls import (
    ModelCallIntent,
    ModelCallReceipt,
    intent_from_dict,
    intent_to_dict,
    receipt_from_dict,
    receipt_to_dict,
)
from linch.permissions.engine import PermissionDecision
from linch.permissions.keys import permission_decision_from_dict, permission_decision_to_dict
from linch.run_store import (
    InMemoryRunStore,
    RunCheckpoint,
    build_run_contract,
    checkpoint_from_dict,
    checkpoint_to_dict,
    create_model_input_snapshot,
    decode_model_input_snapshot,
    run_contract_from_dict,
    run_contract_to_dict,
)
from linch.run_stores.postgres import PostgresRunStore
from linch.sessions.store import ProviderViewSnapshot, snapshot_from_dict, snapshot_to_dict
from linch.types import Message, ProviderRequest, TextBlock, Usage


def _checkpoint() -> RunCheckpoint:
    return RunCheckpoint(phase="started", prompt="hello", turn_index=0, total_usage=Usage())


def _contract():
    return build_run_contract(primary_model="provider/model")


def _intent() -> ModelCallIntent:
    return ModelCallIntent(
        logical_call_id="logical",
        delivery_attempt_id="delivery",
        parent_logical_call_id=None,
        kind="primary",
        run_id="run",
        turn_index=0,
        requested_model="provider/model",
        request_fingerprint="request",
        strategy_id=None,
    )


def _receipt() -> ModelCallReceipt:
    return ModelCallReceipt(
        logical_call_id="logical",
        delivery_attempt_id="delivery",
        kind="primary",
        run_id="run",
        turn_index=0,
        actual_model="provider/model",
        usage_status="reported",
        usage=Usage(input_tokens=1, output_tokens=2),
        cost_usd=None,
        disposition="accepted",
        rejection_reason=None,
        accepted_text=None,
        accepted_text_sha256=None,
        duration_ms=1,
        request_fingerprint="request",
        strategy_id=None,
        source_context_fingerprint=None,
    )


def _request() -> ProviderRequest:
    return ProviderRequest(
        model="provider/model",
        system=[],
        tools=[],
        messages=[Message(role="user", content=[TextBlock(text="hello")])],
    )


def _opaque_event_with_nested_lists(depth: int = 99) -> dict[str, object]:
    value: object = "leaf"
    for _ in range(depth):
        value = [value]
    return {"type": "future_event", "ignorable": True, "payload": value}


def _set_version(raw: dict[str, object], version: object) -> None:
    if version is None:
        raw.pop("schema_version", None)
    else:
        raw["schema_version"] = version


@pytest.mark.parametrize(
    "kind",
    [
        "checkpoint",
        "run_contract",
        "model_intent",
        "model_receipt",
        "model_event",
        "permission",
        "provider_snapshot",
        "model_input_snapshot",
        "stored_event",
    ],
)
@pytest.mark.parametrize("version", [None, 1, 2, 4, True, "3"])
def test_wire_decoders_reject_noncurrent_version(kind: str, version: object) -> None:
    """Reject every pre-3.0 or malformed wire-version marker at its decoder."""
    decoder: Any
    if kind == "checkpoint":
        raw = checkpoint_to_dict(_checkpoint())
        decoder = checkpoint_from_dict
        _set_version(raw, version)
    elif kind == "run_contract":
        raw = run_contract_to_dict(_contract())
        decoder = run_contract_from_dict
        _set_version(raw, version)
    elif kind == "model_intent":
        raw = intent_to_dict(_intent())
        decoder = intent_from_dict
        raw["version"] = version
        if version is None:
            raw.pop("version")
    elif kind == "model_receipt":
        raw = receipt_to_dict(_receipt())
        decoder = receipt_from_dict
        raw["version"] = version
        if version is None:
            raw.pop("version")
    elif kind == "model_event":
        raw = event_to_dict(ModelCallEvent("run", "intent", intent_to_dict(_intent())))
        decoder = event_from_dict
        _set_version(raw, version)
    elif kind == "permission":
        raw = permission_decision_to_dict(PermissionDecision(decision="allow"))
        decoder = permission_decision_from_dict
        _set_version(raw, version)
    elif kind == "provider_snapshot":
        raw = snapshot_to_dict(ProviderViewSnapshot(provider_view=[], covers_seq=0))
        decoder = snapshot_from_dict
        _set_version(raw, version)
    elif kind == "model_input_snapshot":
        snapshot = create_model_input_snapshot("run", 1, _request())
        with pytest.raises(UnsupportedWireVersionError):
            decode_model_input_snapshot(replace(snapshot, codec_version=cast(Any, version)))
        return
    else:
        raw = {
            "schema_version": WIRE_VERSION,
            "event": event_to_dict(ToolCallStartEvent("call", "Search", {}, "s")),
        }
        decoder = _stored_event_from_dict
        _set_version(raw, version)

    with pytest.raises(UnsupportedWireVersionError):
        decoder(raw)


@pytest.mark.asyncio
async def test_stored_events_are_versioned_and_round_trip(
    pg_dsn,
) -> None:
    store = PostgresRunStore(pg_dsn)
    try:
        await store.create_run("session", id="run")
        event = ToolCallStartEvent(tool_use_id="call", tool_name="Search", input={}, summary="s")
        await store.append_event("run", event)
        pool = await store._ensure()
        raw = await pool.fetchval("SELECT event FROM run_events WHERE run_id = 'run'")
        import json

        stored = json.loads(raw)
        assert stored["schema_version"] == WIRE_VERSION
        assert stored["event"] == event_to_dict(event)
        loaded = await store.load_events("run")
        assert loaded[0].event == event
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_stored_ignorable_event_preserves_opaque_schema_fields(
    pg_dsn,
) -> None:
    store = PostgresRunStore(pg_dsn)
    try:
        await store.create_run("session", id="run")
        raw = {
            "type": "future_event",
            "ignorable": True,
            "schema_version": 77,
            "payload": {"nested": [1, {"schema_version": 88}]},
        }
        await store.append_event("run", event_from_dict(raw))
        loaded = await store.load_events("run")
        assert event_to_dict(loaded[0].event) == raw
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_stored_ignorable_event_depth_budget_survives_in_memory_copy() -> None:
    raw = _opaque_event_with_nested_lists()
    store = InMemoryRunStore()
    await store.create_run("session", id="run")
    await store.append_event("run", event_from_dict(raw))

    loaded = await store.load_events("run")

    assert event_to_dict(loaded[0].event) == raw


@pytest.mark.asyncio
async def test_stored_ignorable_event_depth_budget_survives_postgres_append(
    pg_dsn,
) -> None:
    raw = _opaque_event_with_nested_lists()
    store = PostgresRunStore(pg_dsn)
    try:
        await store.create_run("session", id="run")
        await store.append_event("run", event_from_dict(raw))

        loaded = await store.load_events("run")

        assert event_to_dict(loaded[0].event) == raw
    finally:
        await store.close()
