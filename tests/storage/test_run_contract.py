from __future__ import annotations

import json
from pathlib import Path

import pytest

from linch.events import ContextBuildEvent
from linch.run_store import (
    RUN_CONTRACT_META_KEY,
    InMemoryRunStore,
    RunContractMismatchError,
    SqliteRunStore,
    build_run_contract,
    compare_run_contract,
    ensure_run_contract_compatible,
    run_contract_from_dict,
    run_contract_from_meta,
    run_contract_to_dict,
    run_meta_with_contract,
)


def _contract(**overrides):
    values = {
        "primary_model": "provider/model-a",
        "fallback_models": ["provider/model-b"],
        "system_blocks": [{"text": "You are precise.", "cacheable": True}],
        "tool_schemas": [
            {
                "name": "Search",
                "scope": "read",
                "description": "Search things",
                "input_schema": {
                    "type": "object",
                    "properties": {"q": {"type": "string"}},
                    "required": ["q"],
                },
            }
        ],
        "input_payload": {"prompt": "find it", "images": None},
        "output_schema": {
            "name": "Answer",
            "schema": {"type": "object", "properties": {"answer": {"type": "string"}}},
            "strict": True,
        },
        "final_tool_name": None,
        "run_options": {
            "max_output_tokens": 1000,
            "temperature": 0,
            "tool_choice": "auto",
        },
        "budget": {"max_tokens": 5000, "max_cost_usd": 1.0, "warn_ratio": 0.9},
        "policies": {"permission_mode": "default", "max_turns": 20},
    }
    values.update(overrides)
    return build_run_contract(**values)


def test_contract_fingerprint_is_canonical_and_round_trips() -> None:
    first = _contract()
    second = _contract(
        policies={"max_turns": 20, "permission_mode": "default"},
        run_options={"tool_choice": "auto", "temperature": 0, "max_output_tokens": 1000},
    )

    assert first.fingerprint == second.fingerprint
    wire = json.loads(json.dumps(run_contract_to_dict(first)))
    assert run_contract_from_dict(wire) == first


def test_contract_future_envelope_keys_are_forward_tolerant() -> None:
    wire = run_contract_to_dict(_contract())
    wire["future_envelope_key"] = {"ignored": True}

    restored = run_contract_from_dict(wire)

    assert restored.fingerprint == wire["fingerprint"]


def test_contract_mismatch_reports_stable_field_path() -> None:
    stored = _contract()
    tools = list(stored.payload["tools"])
    tools[0] = {**tools[0], "scope": "exec"}
    requested = _contract(tool_schemas=tools)

    comparison = compare_run_contract(stored, requested)

    assert comparison.compatible is False
    assert any(item.path == "$.payload.tools[0].scope" for item in comparison.differences)
    with pytest.raises(RunContractMismatchError, match=r"tools\[0\]\.scope"):
        ensure_run_contract_compatible(stored, requested)


def test_contract_corrupt_fingerprint_fails_closed() -> None:
    wire = run_contract_to_dict(_contract())
    wire["fingerprint"] = "sha256:not-the-content"
    stored = run_contract_from_dict(wire)

    comparison = compare_run_contract(stored, _contract())

    assert comparison.compatible is False
    assert comparison.differences[0].path == "$.fingerprint"


def test_legacy_contract_requires_explicit_unsafe_override() -> None:
    requested = _contract()

    assert compare_run_contract(None, requested).compatible is False
    allowed = compare_run_contract(None, requested, allow_legacy=True)
    assert allowed.compatible is True
    assert allowed.legacy is True


def test_contract_rejects_noncanonical_callable_configuration() -> None:
    with pytest.raises(TypeError, match="not deterministically serializable"):
        _contract(policies={"permission_callback": lambda: None})


async def test_run_meta_contract_is_deep_copied_in_memory() -> None:
    source = {"nested": {"labels": ["original"]}}
    meta = run_meta_with_contract(_contract(), source)
    store = InMemoryRunStore()
    await store.create_run("session-1", id="run-1", meta=meta)

    source["nested"]["labels"].append("source-mutated")
    meta["nested"]["labels"].append("caller-mutated")  # type: ignore[index]
    first = await store.load_run("run-1")
    assert first is not None
    first.meta["nested"]["labels"].append("loaded-mutated")  # type: ignore[index]
    second = await store.load_run("run-1")

    assert second is not None
    assert second.meta["nested"] == {"labels": ["original"]}
    restored = run_contract_from_meta(second.meta)
    assert restored is not None
    assert restored.fingerprint == _contract().fingerprint


async def test_sqlite_persistence_sanitizes_arbitrary_event_metadata(tmp_path: Path) -> None:
    store = SqliteRunStore(tmp_path / "runs.db")
    try:
        meta = run_meta_with_contract(_contract(), {"path": tmp_path, "labels": {"b", "a"}})
        await store.create_run("session-1", id="run-1", meta=meta)
        await store.append_event(
            "run-1",
            ContextBuildEvent(
                system_blocks=1,
                messages=2,
                selected_tools=["Search"],
                budget={},
                metadata={"path": tmp_path, "opaque": object(), "nan": float("nan")},
            ),
        )

        loaded = await store.load_run("run-1")
        events = await store.load_events("run-1")

        assert loaded is not None
        assert loaded.meta["path"] == str(tmp_path)
        assert loaded.meta["labels"] == ["a", "b"]
        assert RUN_CONTRACT_META_KEY in loaded.meta
        event = events[0].event
        assert isinstance(event, ContextBuildEvent)
        assert event.metadata["path"] == str(tmp_path)
        assert isinstance(event.metadata["opaque"], str)
        assert event.metadata["nan"] == "nan"
    finally:
        await store.close()
