from __future__ import annotations

from pathlib import Path

import pytest

from linch.events import ContextBuildEvent
from linch.run_store import (
    RUN_CONTRACT_META_KEY,
    InMemoryRunStore,
    build_run_contract,
    compare_run_contract,
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


@pytest.mark.parametrize("fingerprint", [7])
def test_contract_invalid_fingerprint_fails_closed(fingerprint) -> None:
    wire = run_contract_to_dict(_contract())
    wire["fingerprint"] = fingerprint
    with pytest.raises(ValueError, match="fingerprint"):
        run_contract_from_dict(wire)


def test_contract_writer_does_not_repair_corrupt_fingerprint() -> None:
    from dataclasses import replace

    with pytest.raises(ValueError, match="fingerprint"):
        run_contract_to_dict(replace(_contract(), fingerprint="corrupt"))


def test_missing_contract_rejects_with_start_fresh_version_error() -> None:
    from linch._wire import UnsupportedWireVersionError

    for action in (
        lambda: run_contract_from_meta({}),
        lambda: compare_run_contract(None, _contract()),
    ):
        with pytest.raises(UnsupportedWireVersionError, match="start a new run"):
            action()


def test_removed_legacy_resume_option_is_rejected() -> None:
    from linch import RunOptions

    with pytest.raises(TypeError, match="allow_legacy_resume"):
        RunOptions(allow_legacy_resume=True)


def test_contract_rejects_noncanonical_callable_configuration() -> None:
    with pytest.raises(TypeError, match="not deterministically serializable"):
        _contract(policies={"permission_callback": lambda: None})


def test_contract_rejects_unsupported_budget_object() -> None:
    with pytest.raises(TypeError, match="budget must be a mapping"):
        _contract(budget=object())


async def test_memory_persistence_sanitizes_arbitrary_event_metadata(tmp_path: Path) -> None:
    store = InMemoryRunStore()
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
