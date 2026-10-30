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
    RunCheckpoint,
    SqliteRunStore,
    build_run_contract,
    checkpoint_from_dict,
    checkpoint_to_dict,
    create_model_input_snapshot,
    decode_model_input_snapshot,
    run_contract_from_dict,
    run_contract_to_dict,
)
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


def _set_version(raw: dict[str, object], version: object) -> None:
    if version is None:
        raw.pop("schema_version", None)
    else:
        raw["schema_version"] = version


@pytest.mark.parametrize("version", [None, 1, 2, 4, True, "3"])
def test_checkpoint_requires_exact_wire_version(version: object) -> None:
    raw = checkpoint_to_dict(_checkpoint())
    _set_version(raw, version)

    with pytest.raises(
        UnsupportedWireVersionError, match="state was written by Linch < 3.0.*start a new run"
    ):
        checkpoint_from_dict(raw)


@pytest.mark.parametrize("version", [None, 1, 2, 4, True, "3"])
def test_run_contract_requires_exact_wire_version(version: object) -> None:
    raw = run_contract_to_dict(_contract())
    _set_version(raw, version)

    with pytest.raises(UnsupportedWireVersionError):
        run_contract_from_dict(raw)


@pytest.mark.parametrize(
    "decoder,encoder,value",
    [
        (intent_from_dict, intent_to_dict, _intent),
        (receipt_from_dict, receipt_to_dict, _receipt),
    ],
)
@pytest.mark.parametrize("version", [None, 1, 2, 4, True, "3"])
def test_model_call_records_require_exact_wire_version(decoder, encoder, value, version) -> None:
    raw = encoder(value())
    if version is None:
        raw.pop("version")
    else:
        raw["version"] = version

    with pytest.raises(UnsupportedWireVersionError):
        decoder(raw)


@pytest.mark.parametrize("version", [None, 1, 2, 4, True, "3"])
def test_model_call_event_requires_exact_wire_version(version: object) -> None:
    event = ModelCallEvent("run", "intent", intent_to_dict(_intent()))
    raw = event_to_dict(event)
    _set_version(raw, version)

    with pytest.raises(UnsupportedWireVersionError):
        event_from_dict(raw)


@pytest.mark.parametrize("version", [None, 1, 2, 4, True, "3"])
def test_permission_decision_requires_exact_wire_version(version: object) -> None:
    raw = permission_decision_to_dict(PermissionDecision(decision="allow"))
    _set_version(raw, version)

    with pytest.raises(UnsupportedWireVersionError):
        permission_decision_from_dict(raw)


@pytest.mark.parametrize("version", [None, 1, 2, 4, True, "3"])
def test_provider_snapshot_requires_exact_wire_version(version: object) -> None:
    raw = snapshot_to_dict(ProviderViewSnapshot(provider_view=[], covers_seq=0))
    _set_version(raw, version)

    with pytest.raises(UnsupportedWireVersionError):
        snapshot_from_dict(raw)


@pytest.mark.parametrize("version", [None, 1, 2, 4, True, "3"])
def test_exact_model_input_snapshot_requires_exact_wire_version(version: object) -> None:
    snapshot = create_model_input_snapshot("run", 1, _request())
    snapshot = replace(snapshot, codec_version=cast(Any, version))

    with pytest.raises(UnsupportedWireVersionError):
        decode_model_input_snapshot(snapshot)


@pytest.mark.parametrize("version", [None, 1, 2, 4, True, "3"])
def test_stored_event_envelope_requires_exact_wire_version(version: object) -> None:
    raw: dict[str, object] = {
        "schema_version": WIRE_VERSION,
        "event": event_to_dict(ToolCallStartEvent("call", "Search", {}, "s")),
    }
    _set_version(raw, version)

    with pytest.raises(UnsupportedWireVersionError):
        _stored_event_from_dict(raw)


@pytest.mark.asyncio
async def test_stored_events_are_versioned_and_round_trip(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    try:
        await store.create_run("session", id="run")
        event = ToolCallStartEvent(tool_use_id="call", tool_name="Search", input={}, summary="s")
        await store.append_event("run", event)
        raw = await store._exec.run(
            lambda conn: conn.execute(
                "select event from run_events where run_id = 'run'"
            ).fetchone()[0]
        )
        import json

        stored = json.loads(raw)
        assert stored["schema_version"] == WIRE_VERSION
        assert stored["event"] == event_to_dict(event)
        loaded = await store.load_events("run")
        assert loaded[0].event == event
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_stored_ignorable_event_preserves_opaque_schema_fields(tmp_path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
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
