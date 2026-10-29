"""Private durable controls for deferred tool-surface discovery.

This module deliberately contains only the shared persistence boundary.  It
does not offer tools, mutate agent configuration, or expose a public API.
"""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy

from .errors import ConfigError
from .types import ContentBlock, Message, ToolActivation, ToolResultBlock, ToolSurfaceReaderGuard

_TOOL_ACTIVATION_SCHEMA_VERSION = 1
_TOOL_SURFACE_READER_VERSION = 1
_TOOL_SURFACE_FEATURE_VERSION = 1
_TOOL_SEARCH_LOADER_ID = "linch.tool_search.v1"

_ACTIVATION_KEYS = frozenset(
    {
        "schema_version",
        "name",
        "activation_id",
        "activation_version",
        "tool_contract_fingerprint",
        "scope_id",
        "scope_fingerprint",
        "catalog_policy_id",
        "catalog_policy_version",
        "activation_policy_id",
        "activation_policy_version",
        "source_loader_id",
        "source_tool_use_id",
    }
)
_GUARD_KEYS = frozenset({"type", "required_reader_version", "feature_version"})


def _require_object_with_exact_keys(
    raw: object,
    *,
    keys: frozenset[str],
    name: str,
) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise ValueError(f"{name} must be an object")
    if set(raw) != keys:
        raise ValueError(f"{name} has missing or unknown fields")
    return raw


def _require_string(raw: dict[str, object], key: str, *, name: str) -> str:
    value = raw[key]
    if not isinstance(value, str):
        raise ValueError(f"{name}.{key} must be a string")
    return value


def _require_known_version(raw: dict[str, object], key: str, *, expected: int, name: str) -> int:
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name}.{key} must be an integer")
    if value != expected:
        raise ValueError(f"unsupported {name}.{key}: {value!r}")
    return value


def _tool_activation_to_dict(activation: ToolActivation) -> dict[str, object]:
    """Encode one activation after enforcing the versioned durable schema."""

    if not isinstance(activation, ToolActivation):
        raise ValueError("tool activation must be a ToolActivation")
    raw: dict[str, object] = {
        "schema_version": activation.schema_version,
        "name": activation.name,
        "activation_id": activation.activation_id,
        "activation_version": activation.activation_version,
        "tool_contract_fingerprint": activation.tool_contract_fingerprint,
        "scope_id": activation.scope_id,
        "scope_fingerprint": activation.scope_fingerprint,
        "catalog_policy_id": activation.catalog_policy_id,
        "catalog_policy_version": activation.catalog_policy_version,
        "activation_policy_id": activation.activation_policy_id,
        "activation_policy_version": activation.activation_policy_version,
        "source_loader_id": activation.source_loader_id,
        "source_tool_use_id": activation.source_tool_use_id,
    }
    _tool_activation_from_dict(raw)
    return raw


def _tool_activation_from_dict(raw: object) -> ToolActivation:
    """Decode exactly the activation schema supported by this reader."""

    values = _require_object_with_exact_keys(raw, keys=_ACTIVATION_KEYS, name="tool activation")
    schema_version = _require_known_version(
        values,
        "schema_version",
        expected=_TOOL_ACTIVATION_SCHEMA_VERSION,
        name="tool activation",
    )
    activation_version = values["activation_version"]
    if activation_version is not None and not isinstance(activation_version, str):
        raise ValueError("tool activation.activation_version must be a string or null")
    source_loader_id = _require_string(values, "source_loader_id", name="tool activation")
    if source_loader_id != _TOOL_SEARCH_LOADER_ID:
        raise ValueError(f"unsupported tool activation source loader: {source_loader_id!r}")
    return ToolActivation(
        schema_version=schema_version,
        name=_require_string(values, "name", name="tool activation"),
        activation_id=_require_string(values, "activation_id", name="tool activation"),
        activation_version=activation_version,
        tool_contract_fingerprint=_require_string(
            values, "tool_contract_fingerprint", name="tool activation"
        ),
        scope_id=_require_string(values, "scope_id", name="tool activation"),
        scope_fingerprint=_require_string(values, "scope_fingerprint", name="tool activation"),
        catalog_policy_id=_require_string(values, "catalog_policy_id", name="tool activation"),
        catalog_policy_version=_require_string(
            values, "catalog_policy_version", name="tool activation"
        ),
        activation_policy_id=_require_string(
            values, "activation_policy_id", name="tool activation"
        ),
        activation_policy_version=_require_string(
            values, "activation_policy_version", name="tool activation"
        ),
        source_loader_id=source_loader_id,
        source_tool_use_id=_require_string(values, "source_tool_use_id", name="tool activation"),
    )


def _tool_activations_to_wire(value: object) -> list[dict[str, object]]:
    """Encode the optional ``ToolResultBlock`` field without changing old empty wires."""

    if not isinstance(value, tuple):
        raise ValueError("tool_activations must be a tuple")
    return [_tool_activation_to_dict(activation) for activation in value]


def _tool_activations_from_wire(raw: object) -> tuple[ToolActivation, ...]:
    """Decode a present activation field strictly; absent remains legacy-empty."""

    if not isinstance(raw, list):
        raise ValueError("tool_activations must be a list")
    return tuple(_tool_activation_from_dict(value) for value in raw)


def _tool_surface_guard_to_dict(guard: ToolSurfaceReaderGuard) -> dict[str, object]:
    """Encode the old-reader barrier after enforcing its fixed version contract."""

    if not isinstance(guard, ToolSurfaceReaderGuard):
        raise ValueError("tool surface guard must be a ToolSurfaceReaderGuard")
    raw: dict[str, object] = {
        "type": guard.type,
        "required_reader_version": guard.required_reader_version,
        "feature_version": guard.feature_version,
    }
    _tool_surface_guard_from_dict(raw)
    return raw


def _tool_surface_guard_from_dict(raw: object) -> ToolSurfaceReaderGuard:
    """Decode exactly the guard version this binary knows how to enforce."""

    values = _require_object_with_exact_keys(raw, keys=_GUARD_KEYS, name="tool surface guard")
    if values["type"] != "tool_surface_guard":
        raise ValueError("invalid tool surface guard type")
    required_reader_version = _require_known_version(
        values,
        "required_reader_version",
        expected=_TOOL_SURFACE_READER_VERSION,
        name="tool surface guard",
    )
    feature_version = _require_known_version(
        values,
        "feature_version",
        expected=_TOOL_SURFACE_FEATURE_VERSION,
        name="tool surface guard",
    )
    return ToolSurfaceReaderGuard(
        required_reader_version=required_reader_version,
        feature_version=feature_version,
    )


def _project_message_for_provider(message: Message) -> Message | None:
    """Detach and sanitize one message for a provider-facing projection.

    Controls are validated before being removed.  A message that was already
    empty stays present; only one made empty by removing guard blocks is dropped.
    """

    projected_content: list[ContentBlock] = []
    removed_control = False
    for block in message.content:
        if isinstance(block, ToolSurfaceReaderGuard):
            _tool_surface_guard_to_dict(block)
            removed_control = True
            continue
        if isinstance(block, ToolResultBlock):
            _tool_activations_to_wire(block.tool_activations)
            if block.tool_activations:
                projected_content.append(
                    ToolResultBlock(
                        tool_use_id=block.tool_use_id,
                        content=deepcopy(block.content),
                        is_error=block.is_error,
                        tool_activations=(),
                        type=block.type,
                    )
                )
            else:
                projected_content.append(deepcopy(block))
            continue
        projected_content.append(deepcopy(block))

    if message.content and not projected_content and removed_control:
        return None
    return Message(
        role=message.role,
        content=projected_content,
        provider_metadata=deepcopy(message.provider_metadata),
    )


def _project_messages_for_provider(messages: Iterable[Message]) -> list[Message]:
    """Apply the control-safe provider projection to every source message."""

    projected_messages: list[Message] = []
    for message in messages:
        projected = _project_message_for_provider(message)
        if projected is not None:
            projected_messages.append(projected)
    return projected_messages


def _require_runtime_control_block_codec(store: object) -> int:
    """Require a store to explicitly advertise durable control-block preservation."""

    version = getattr(store, "runtime_control_block_codec_version", None)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ConfigError("tool discovery requires runtime_control_block_codec_version >= 1")
    return version
