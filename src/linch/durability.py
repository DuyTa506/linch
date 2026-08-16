from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any, TypeAlias

from linch.errors import ConfigError
from linch.types import Message, message_from_dict, message_to_dict

JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


def _strict_json(value: object, *, label: str) -> str:
    """Return a deterministic JSON encoding or reject the value.

    ``json.dumps`` already detects reference cycles.  ``allow_nan=False`` and
    ``skipkeys=False`` close the two other common holes in "JSON-safe" checks:
    non-finite floats and silently discarded non-string mapping keys.
    """

    def validate(item: object, seen: set[int]) -> None:
        if item is None or isinstance(item, bool | int | str):
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ConfigError(f"{label} must not contain NaN or Infinity")
            return
        if not isinstance(item, list | dict):
            raise ConfigError(
                f"{label} must be strictly JSON-serializable; got {type(item).__name__}"
            )
        identity = id(item)
        if identity in seen:
            raise ConfigError(f"{label} must not contain reference cycles")
        seen.add(identity)
        try:
            if isinstance(item, list):
                for child in item:
                    validate(child, seen)
            else:
                for key, child in item.items():
                    if not isinstance(key, str):
                        raise ConfigError(f"{label} must contain only string object keys")
                    validate(child, seen)
        finally:
            seen.remove(identity)

    validate(value, set())
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            skipkeys=False,
        )
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{label} must be strictly JSON-serializable: {exc}") from exc


@dataclass(frozen=True, slots=True)
class DurabilityOptions:
    """Opt-in durability capabilities for an agent.

    Defaults deliberately preserve Linch 2.x behavior.  ``strict_v1`` is a
    named, versioned profile so a future release cannot silently broaden what
    callers opted into.
    """

    durable_inbox: bool = False
    exact_model_input: bool = False
    delivery_lease_s: float = 30.0

    def __post_init__(self) -> None:
        if not isinstance(self.durable_inbox, bool):
            raise ConfigError("durable_inbox must be a bool")
        if not isinstance(self.exact_model_input, bool):
            raise ConfigError("exact_model_input must be a bool")
        if isinstance(self.delivery_lease_s, bool) or not isinstance(
            self.delivery_lease_s, int | float
        ):
            raise ConfigError("delivery_lease_s must be a positive finite number")
        if self.delivery_lease_s <= 0 or not math.isfinite(float(self.delivery_lease_s)):
            raise ConfigError("delivery_lease_s must be a positive finite number")

    @classmethod
    def strict_v1(cls) -> DurabilityOptions:
        return cls(durable_inbox=True, exact_model_input=True)


@dataclass(frozen=True, slots=True)
class InboxDelivery:
    """A stable, deduplicated message destined for a session's next turn."""

    delivery_id: str
    message: Message
    source: str = "host"
    metadata: dict[str, JsonValue] = field(default_factory=dict)
    digest: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.delivery_id, str) or not self.delivery_id:
            raise ConfigError("InboxDelivery.delivery_id must be a non-empty string")
        if not isinstance(self.source, str) or not self.source:
            raise ConfigError("InboxDelivery.source must be a non-empty string")
        if not isinstance(self.message, Message):
            raise ConfigError("InboxDelivery.message must be a Message")
        if self.message.role != "user":
            raise ConfigError("InboxDelivery.message must have role='user'")
        if not isinstance(self.metadata, dict) or any(
            not isinstance(key, str) for key in self.metadata
        ):
            raise ConfigError("InboxDelivery.metadata must be a dict with string keys")

        message_json = _strict_json(message_to_dict(self.message), label="InboxDelivery.message")
        metadata_json = _strict_json(self.metadata, label="InboxDelivery.metadata")

        # Detach nested mutable inputs.  Otherwise a caller could enqueue a
        # delivery and mutate its payload while its digest remains unchanged.
        object.__setattr__(self, "message", message_from_dict(json.loads(message_json)))
        object.__setattr__(self, "metadata", json.loads(metadata_json))
        envelope = _delivery_envelope(message_json, self.source, metadata_json)
        object.__setattr__(self, "digest", hashlib.sha256(envelope).hexdigest())


def encode_inbox_delivery(delivery: InboxDelivery) -> tuple[str, str, str]:
    """Return canonical message, source and metadata fields for store backends."""

    message_json = _strict_json(message_to_dict(delivery.message), label="InboxDelivery.message")
    metadata_json = _strict_json(delivery.metadata, label="InboxDelivery.metadata")
    current_digest = hashlib.sha256(
        _delivery_envelope(message_json, delivery.source, metadata_json)
    ).hexdigest()
    if current_digest != delivery.digest:
        raise ConfigError("InboxDelivery was mutated after construction")
    return message_json, delivery.source, metadata_json


def _delivery_envelope(message_json: str, source: str, metadata_json: str) -> bytes:
    return _strict_json(
        {
            "schema_version": 1,
            "message": json.loads(message_json),
            "source": source,
            "metadata": json.loads(metadata_json),
        },
        label="InboxDelivery",
    ).encode()


def decode_inbox_message(payload: str) -> Message:
    raw: Any = json.loads(payload)
    if not isinstance(raw, dict):  # pragma: no cover - store corruption guard
        raise ConfigError("stored inbox message is not a JSON object")
    return message_from_dict(raw)
