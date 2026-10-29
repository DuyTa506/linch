"""Durable control-field codec and provider-projection behavior."""

from __future__ import annotations

import json
from typing import get_args

import pytest

from linch.errors import ConfigError
from linch.sessions.memory import InMemorySessionStore
from linch.sessions.postgres import PostgresSessionStore
from linch.sessions.sqlite import SqliteSessionStore
from linch.sessions.store import ProviderViewSnapshot, snapshot_from_dict, snapshot_to_dict
from linch.tool_surface import (
    ToolSurfaceReaderGuard,
    _require_runtime_control_block_codec,
)
from linch.types import (
    ContentBlock,
    Message,
    ToolActivation,
    ToolResultBlock,
    block_to_dict,
    message_from_dict,
    message_to_dict,
)


def _activation() -> ToolActivation:
    return ToolActivation(
        schema_version=1,
        name="deferred_tool",
        activation_id="catalog:deferred_tool",
        activation_version="2026-10-30",
        tool_contract_fingerprint="tool-fingerprint",
        scope_id="scope-1",
        scope_fingerprint="scope-fingerprint",
        catalog_policy_id="catalog-policy",
        catalog_policy_version="1",
        activation_policy_id="activation-policy",
        activation_policy_version="2",
        source_loader_id="linch.tool_search.v1",
        source_tool_use_id="call-tool-search",
    )


def _guard() -> ToolSurfaceReaderGuard:
    return ToolSurfaceReaderGuard(required_reader_version=1, feature_version=1)


def _control_message() -> Message:
    return Message(
        role="user",
        content=[
            ToolResultBlock(
                tool_use_id="call-tool-search",
                content="activated deferred_tool",
                tool_activations=(_activation(),),
            ),
            _guard(),
        ],
        provider_metadata={"unrelated": {"preserve": True}},
    )


def _pre_feature_block_from_dict(raw: dict[str, object]) -> None:
    """The pre-feature reader's closed set of persisted content types."""

    if raw.get("type") not in {
        "text",
        "tool_use",
        "tool_result",
        "thinking",
        "redacted_thinking",
        "image",
    }:
        raise ValueError(f"unknown content block type: {raw.get('type')!r}")


def test_control_message_wire_round_trip_preserves_every_durable_fact() -> None:
    message = _control_message()

    raw = message_to_dict(message)

    assert raw == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "call-tool-search",
                "content": "activated deferred_tool",
                "tool_activations": [
                    {
                        "schema_version": 1,
                        "name": "deferred_tool",
                        "activation_id": "catalog:deferred_tool",
                        "activation_version": "2026-10-30",
                        "tool_contract_fingerprint": "tool-fingerprint",
                        "scope_id": "scope-1",
                        "scope_fingerprint": "scope-fingerprint",
                        "catalog_policy_id": "catalog-policy",
                        "catalog_policy_version": "1",
                        "activation_policy_id": "activation-policy",
                        "activation_policy_version": "2",
                        "source_loader_id": "linch.tool_search.v1",
                        "source_tool_use_id": "call-tool-search",
                    }
                ],
            },
            {
                "type": "tool_surface_guard",
                "required_reader_version": 1,
                "feature_version": 1,
            },
        ],
        "provider_metadata": {"unrelated": {"preserve": True}},
    }
    assert message_from_dict(raw) == message

    ordinary = message_to_dict(
        Message(
            role="user",
            content=[ToolResultBlock(tool_use_id="ordinary", content="ordinary result")],
        )
    )
    assert ordinary["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "ordinary",
        "content": "ordinary result",
    }


def test_legacy_tool_result_positional_type_keeps_an_empty_activation_field() -> None:
    result = ToolResultBlock("ordinary", "ordinary result", False, "tool_result")

    assert result.type == "tool_result"
    assert result.tool_activations == ()
    assert block_to_dict(result) == {
        "type": "tool_result",
        "tool_use_id": "ordinary",
        "content": "ordinary result",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", True),
        ("schema_version", 2),
        ("source_loader_id", "different.loader"),
    ],
)
def test_control_message_rejects_invalid_or_unknown_activation_versions(
    field: str,
    value: object,
) -> None:
    raw = message_to_dict(_control_message())
    raw["content"][0]["tool_activations"][0] = {
        **raw["content"][0]["tool_activations"][0],
        field: value,
    }

    with pytest.raises(ValueError):
        message_from_dict(raw)


def test_control_message_rejects_a_missing_or_malformed_activation_field() -> None:
    missing_version = message_to_dict(_control_message())
    del missing_version["content"][0]["tool_activations"][0]["schema_version"]
    with pytest.raises(ValueError):
        message_from_dict(missing_version)

    malformed_field = message_to_dict(_control_message())
    malformed_field["content"][0]["tool_activations"] = "not a list"
    with pytest.raises(ValueError):
        message_from_dict(malformed_field)


@pytest.mark.parametrize(
    "raw_guard",
    [
        {"type": "tool_surface_guard", "required_reader_version": True, "feature_version": 1},
        {"type": "tool_surface_guard", "required_reader_version": 2, "feature_version": 1},
        {"type": "tool_surface_guard", "required_reader_version": 1, "feature_version": True},
        {"type": "tool_surface_guard", "required_reader_version": 1, "feature_version": 2},
        {"type": "tool_surface_guard", "feature_version": 1},
        {"type": "tool_surface_guard", "required_reader_version": 1},
    ],
)
def test_guard_decoder_rejects_invalid_or_unknown_required_versions(
    raw_guard: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        message_from_dict({"role": "user", "content": [raw_guard]})


def test_guard_wire_type_stops_a_pre_feature_reader() -> None:
    raw_guard = block_to_dict(_guard())

    with pytest.raises(ValueError, match="unknown content block type"):
        _pre_feature_block_from_dict(raw_guard)


def test_guard_is_a_runtime_content_block() -> None:
    assert ToolSurfaceReaderGuard in get_args(ContentBlock)


def test_provider_snapshot_projects_controls_and_preserves_projection_identity() -> None:
    snapshot = ProviderViewSnapshot(
        provider_view=[_control_message()],
        covers_seq=7,
        projection_id="projection-7",
    )

    raw = snapshot_to_dict(snapshot)
    restored = snapshot_from_dict(raw)

    assert raw["projection_id"] == "projection-7"
    assert raw["provider_view"] == [
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call-tool-search",
                    "content": "activated deferred_tool",
                }
            ],
            "provider_metadata": {"unrelated": {"preserve": True}},
        }
    ]
    assert restored.projection_id == "projection-7"
    result = restored.provider_view[0].content[0]
    assert isinstance(result, ToolResultBlock)
    assert result.tool_activations == ()


def test_runtime_control_codec_capability_requires_an_explicit_supported_version() -> None:
    class MissingCapability:
        pass

    class InvalidCapability:
        runtime_control_block_codec_version = True

    class OldCapability:
        runtime_control_block_codec_version = 0

    class FutureCapability:
        runtime_control_block_codec_version = 2

    for store in (MissingCapability(), InvalidCapability(), OldCapability()):
        with pytest.raises(ConfigError):
            _require_runtime_control_block_codec(store)

    assert _require_runtime_control_block_codec(FutureCapability()) == 2
    assert _require_runtime_control_block_codec(InMemorySessionStore()) == 1
    assert SqliteSessionStore.runtime_control_block_codec_version == 1
    assert PostgresSessionStore.runtime_control_block_codec_version == 1


async def test_postgres_snapshot_codec_projects_controls_without_a_live_connection(
    monkeypatch,
) -> None:
    import linch.sessions.postgres as postgres

    class Connection:
        def __init__(self) -> None:
            self.executed: tuple[object, ...] | None = None

        async def execute(self, *args: object) -> None:
            self.executed = args

    class AcquiredConnection:
        async def __aenter__(self) -> Connection:
            return connection

        async def __aexit__(self, *_: object) -> None:
            return None

    class Pool:
        def acquire(self) -> AcquiredConnection:
            return AcquiredConnection()

    connection = Connection()
    monkeypatch.setattr(postgres, "_import_asyncpg", lambda: object())
    store = postgres.PostgresSessionStore("postgres://unused", pool=Pool())
    store._initialized = True

    await store.save_provider_snapshot(
        "session",
        ProviderViewSnapshot(provider_view=[_control_message()], covers_seq=1, projection_id="pg"),
    )

    assert connection.executed is not None
    encoded_payload = connection.executed[2]
    assert isinstance(encoded_payload, str)
    payload = json.loads(encoded_payload)
    assert payload["projection_id"] == "pg"
    assert payload["provider_view"][0]["content"] == [
        {
            "type": "tool_result",
            "tool_use_id": "call-tool-search",
            "content": "activated deferred_tool",
        }
    ]
