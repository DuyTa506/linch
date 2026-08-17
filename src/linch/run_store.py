from __future__ import annotations

import base64
import hashlib
import json
import math
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from uuid import UUID, uuid4

from .events import (
    IGNORABLE_MAX_DEPTH,
    Event,
    event_from_dict,
    event_to_dict,
    usage_from_dict,
    usage_to_dict,
)
from .sessions.memory import now_iso
from .storage._executor import SqliteExecutor
from .types import (
    Message,
    OutputSchema,
    ProviderRequest,
    SystemBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
    block_from_dict,
    block_to_dict,
    message_from_dict,
    message_to_dict,
)

# Wire-format version for the serialized RunCheckpoint and stored-event log.
# Bump only on a breaking change to the persisted shape. `checkpoint_from_dict`
# reads any version best-effort (unknown future keys are ignored). Stored events
# are required on read unless their envelope explicitly declares them ignorable.
SCHEMA_VERSION = 1

# A run contract is versioned independently from the checkpoint wire format.
# Checkpoints describe *where* execution stopped; this contract describes the
# execution inputs that must stay stable when that checkpoint is resumed.
RUN_CONTRACT_SCHEMA_VERSION = 1
RUN_CONTRACT_META_KEY = "run_contract"

# Model input snapshots have their own codec version because their compatibility
# requirement is stricter than the best-effort checkpoint/event readers.  A
# pending provider request must either decode exactly or fail closed.
MODEL_INPUT_CODEC_VERSION = 1

# "suspended" is a workflow parked at a wf.interrupt; distinct from
# "waiting_permission", which belongs to the tool-permission flow.
RunStatus = Literal["running", "waiting_permission", "suspended", "completed", "failed", "aborted"]
RunPhase = Literal[
    "started",
    "user_appended",
    "provider_pending",
    "assistant_appended",
    "permission_pending",
    "tool_batch_pending",
    "tool_executing",
    "tool_results_appended",
    "turn_complete",
    "workflow_suspended",
    "completed",
    "failed",
    "aborted",
]


@dataclass(slots=True)
class RunCheckpoint:
    phase: RunPhase
    prompt: str
    turn_index: int
    total_usage: Usage
    assistant_message: Message | None = None
    pending_tool_blocks: list[ToolUseBlock] = field(default_factory=list)
    completed_tool_results: dict[str, ToolResultBlock] = field(default_factory=dict)
    force_final_pending: bool = False
    loop_guard_state: dict[str, object] | None = None
    pending_skill_overlay: dict[str, object] | None = None
    current_turn_allowed_tools: list[str] | None = None
    assistant_stop_reason: str | None = None
    permission_decisions: dict[str, dict] = field(default_factory=dict)
    background_workers: dict[str, dict[str, object]] = field(default_factory=dict)
    truncation_attempts: int = 0
    truncation_prefix: str = ""
    pending_truncation_feedback: str | None = None
    tool_batch_event_after_seq: int | None = None
    """Event-log watermark: the seq just before this turn's tool-batch start
    events. On resume, tool-result recovery scans only events after this cursor.
    ``None`` (legacy checkpoints / non-tool phases) triggers the boundary
    fallback in ``_recover_completed_tool_results``."""
    pending_alignment: list[dict[str, Any]] = field(default_factory=list)
    """Undrained ``session.align()`` entries (``{"prompt", "images"}``) at the
    time of this save, restored on resume so steering intent survives a crash.
    Best-effort at-least-once: an entry enqueued after the last save is lost on
    crash; an entry drained just before a crash may be injected once more on
    resume."""
    extension_state: dict[str, Any] = field(default_factory=dict)
    """Opaque, JSON-safe state owned by optional runtime extensions.

    Keys are extension-owned namespaces (for example a checkpointable hook's
    ``checkpoint_key``).  Core preserves entries it does not understand, so
    independently installed extensions can share one durable checkpoint.  A
    checkpoint written before this field existed restores as an empty mapping.
    """
    provider_attempt: int = 0
    """1-based provider dispatch attempt for an exact model-input snapshot.

    Zero is the legacy/default value and is omitted from the checkpoint wire
    shape so runs without exact-input durability keep their existing payload.
    """
    model_input_snapshot_id: str | None = None
    """Snapshot referenced by a ``provider_pending`` checkpoint, when enabled."""


@dataclass(slots=True)
class RunRecord:
    id: str
    session_id: str
    status: RunStatus
    created_at: str
    updated_at: str
    checkpoint: RunCheckpoint | None = None
    meta: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class StoredRunEvent:
    seq: int
    appended_at: str
    event: Event


@dataclass(frozen=True, slots=True)
class ModelInputSnapshot:
    """Immutable encoded provider request captured immediately before dispatch.

    ``request_json`` is canonical JSON rather than a mutable ``ProviderRequest``
    graph.  Use :func:`decode_model_input_snapshot` to integrity-check it and
    recreate a request with the current process's live abort signal.
    """

    id: str
    run_id: str
    provider_attempt: int
    codec_version: int
    request_json: str
    integrity_hash: str
    created_at: str


class ModelInputSnapshotError(ValueError):
    """Raised when an exact model-input snapshot cannot be trusted or decoded."""


@dataclass(frozen=True, slots=True)
class RunContract:
    """Canonical, fingerprinted inputs that define resume-safe execution.

    ``payload`` is deliberately an open mapping.  Newer Linch versions may add
    keys without making older stores unable to decode a run record.  The
    schema version participates in the fingerprint so two meanings of the same
    shape can never compare equal accidentally.
    """

    schema_version: int
    payload: dict[str, Any]
    fingerprint: str


@dataclass(frozen=True, slots=True)
class RunContractDifference:
    """One actionable difference between a stored and requested contract."""

    path: str
    stored: Any
    requested: Any


@dataclass(frozen=True, slots=True)
class RunContractComparison:
    """Result of checking whether a durable run can be resumed safely."""

    compatible: bool
    legacy: bool
    stored_fingerprint: str | None
    requested_fingerprint: str
    differences: tuple[RunContractDifference, ...] = ()

    def describe(self) -> str:
        if self.legacy:
            return "legacy run has no persisted run contract"
        if self.compatible:
            return "run contract matches"
        details = ", ".join(
            f"{item.path}: stored={item.stored!r}, requested={item.requested!r}"
            for item in self.differences[:8]
        )
        if len(self.differences) > 8:
            details += f", and {len(self.differences) - 8} more"
        return (
            "run contract mismatch "
            f"({self.stored_fingerprint or 'missing'} != {self.requested_fingerprint})"
            + (f": {details}" if details else "")
        )


class RunContractMismatchError(ValueError):
    """Raised when resume inputs differ from the inputs of the original run."""

    def __init__(self, comparison: RunContractComparison) -> None:
        self.comparison = comparison
        super().__init__(comparison.describe())


class RunStore(Protocol):
    async def create_run(
        self,
        session_id: str,
        *,
        id: str | None = None,
        meta: dict[str, object] | None = None,
    ) -> RunRecord: ...

    async def load_run(self, run_id: str) -> RunRecord | None: ...

    async def save_checkpoint(
        self,
        run_id: str,
        checkpoint: RunCheckpoint,
        *,
        status: str = "running",
    ) -> RunRecord: ...

    async def append_event(self, run_id: str, event: Event) -> int: ...

    async def load_events(self, run_id: str, *, after_seq: int = 0) -> list[StoredRunEvent]: ...

    async def mark_completed(self, run_id: str, checkpoint: RunCheckpoint) -> RunRecord: ...

    async def mark_failed(
        self,
        run_id: str,
        checkpoint: RunCheckpoint | None = None,
        error: dict[str, object] | None = None,
    ) -> RunRecord: ...


class RunEventBatchStore(Protocol):
    """Optional run-store capability: append several events atomically.

    Detected at runtime with ``getattr`` (a store implementing only ``RunStore``
    keeps working via per-event ``append_event``). Returns the 1-based seq
    assigned to each event, in order — parallel to ``append_event -> int``.
    """

    async def append_events(self, run_id: str, events: list[Event]) -> list[int]: ...


class ModelInputSnapshotStore(Protocol):
    """Optional run-store capability for exact pending provider requests."""

    async def save(
        self,
        run_id: str,
        provider_attempt: int,
        request: ProviderRequest,
    ) -> ModelInputSnapshot: ...

    async def load(self, snapshot_id: str) -> ModelInputSnapshot | None: ...

    async def delete(self, snapshot_id: str) -> None: ...

    async def prune(self, run_id: str, keep_ids: Sequence[str] = ()) -> int: ...


# Single source of truth with the ignorable-event validator: a value this
# codec would truncate must never have been accepted into an event.
_JSON_SAFE_MAX_DEPTH = IGNORABLE_MAX_DEPTH


def _json_safe(value: Any, *, strict: bool = False) -> Any:
    """Return an isolated JSON-safe representation of arbitrary runtime data.

    Persistence uses the permissive mode so an otherwise useful event is not
    lost because extension-owned metadata contains a ``Path`` or dataclass.
    Run-contract construction uses strict mode: an unstable ``repr`` must
    never silently become part of a durability fingerprint.
    """

    return _json_safe_inner(value, strict=strict, seen=frozenset(), depth=0)


def _json_safe_inner(
    value: Any,
    *,
    strict: bool,
    seen: frozenset[int],
    depth: int,
) -> Any:
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return _json_safe_inner(value.value, strict=strict, seen=seen, depth=depth)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Path | UUID):
        return str(value)
    if isinstance(value, bytes | bytearray | memoryview):
        return {"$bytes_base64": base64.b64encode(bytes(value)).decode("ascii")}

    is_recursive_value = (is_dataclass(value) and not isinstance(value, type)) or isinstance(
        value, Mapping | set | frozenset | Sequence
    )
    if is_recursive_value:
        if id(value) in seen:
            if strict:
                raise TypeError("run contract contains a recursive value")
            return "<recursion>"
        if depth >= _JSON_SAFE_MAX_DEPTH:
            if strict:
                raise TypeError(
                    f"run contract exceeds maximum nesting depth {_JSON_SAFE_MAX_DEPTH}"
                )
            return "<max-depth>"
        seen = seen | {id(value)}
        depth += 1

    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _json_safe_inner(
                getattr(value, item.name), strict=strict, seen=seen, depth=depth
            )
            for item in fields(value)
        }
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        reserved_string_keys = {key for key in value if isinstance(key, str)}
        for key, item in value.items():
            if strict and not isinstance(key, str):
                raise TypeError(f"run contract mapping key must be str, got {type(key).__name__}")
            name = str(key)
            if name in out or (not isinstance(key, str) and name in reserved_string_keys):
                if strict:
                    raise TypeError(
                        f"run contract mapping keys collide after normalization: {name!r}"
                    )
                base = f"{name}~{type(key).__name__}"
                name = base
                discriminator = 2
                while name in out or name in reserved_string_keys:
                    name = f"{base}~{discriminator}"
                    discriminator += 1
            out[name] = _json_safe_inner(item, strict=strict, seen=seen, depth=depth)
        return out
    if isinstance(value, set | frozenset):
        items = [_json_safe_inner(item, strict=strict, seen=seen, depth=depth) for item in value]
        return sorted(items, key=canonical_json)
    if isinstance(value, Sequence):
        return [_json_safe_inner(item, strict=strict, seen=seen, depth=depth) for item in value]
    if strict:
        raise TypeError(
            f"run contract value of type {type(value).__name__} "
            "is not deterministically serializable"
        )
    return str(value)


def canonical_json(value: Any) -> str:
    """Serialize ``value`` deterministically for signatures and snapshots."""

    safe = _json_safe(value, strict=True)
    return json.dumps(
        safe,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


_MODEL_INPUT_REQUEST_FIELDS = frozenset(
    {
        "model",
        "system",
        "tools",
        "messages",
        "signal",
        "max_output_tokens",
        "temperature",
        "stop_sequences",
        "max_retries",
        "reasoning",
        "cache_prompt",
        "cache_ttl",
        "thinking",
        "effort",
        "output_schema",
        "tool_choice",
        "stream_partials",
    }
)
_MODEL_INPUT_PAYLOAD_FIELDS = _MODEL_INPUT_REQUEST_FIELDS - {"signal"}


def _strict_model_json(
    value: Any,
    *,
    path: str = "$",
    seen: frozenset[int] = frozenset(),
    depth: int = 0,
) -> Any:
    """Normalize a JSON value without lossy persistence fallbacks."""

    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError(f"{path} contains a non-finite float")
        return value
    if depth >= _JSON_SAFE_MAX_DEPTH:
        raise TypeError(f"{path} exceeds maximum nesting depth {_JSON_SAFE_MAX_DEPTH}")
    if isinstance(value, Mapping):
        if id(value) in seen:
            raise TypeError(f"{path} contains a recursive value")
        nested_seen = seen | {id(value)}
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path} mapping key must be str, got {type(key).__name__}")
            out[key] = _strict_model_json(
                item,
                path=f"{path}.{key}",
                seen=nested_seen,
                depth=depth + 1,
            )
        return out
    if isinstance(value, list):
        if id(value) in seen:
            raise TypeError(f"{path} contains a recursive value")
        nested_seen = seen | {id(value)}
        return [
            _strict_model_json(
                item,
                path=f"{path}[{index}]",
                seen=nested_seen,
                depth=depth + 1,
            )
            for index, item in enumerate(value)
        ]
    raise TypeError(f"{path} contains non-JSON value of type {type(value).__name__}")


def _output_schema_to_snapshot(schema: OutputSchema | None) -> dict[str, Any] | None:
    if schema is None:
        return None
    if not isinstance(schema, OutputSchema):
        raise TypeError("ProviderRequest.output_schema must be OutputSchema or None")
    return {
        "name": schema.name,
        "schema": schema.schema,
        "strict": schema.strict,
        "description": schema.description,
    }


def _provider_request_payload(request: ProviderRequest) -> dict[str, Any]:
    if not isinstance(request, ProviderRequest):
        raise TypeError("request must be a ProviderRequest")
    actual_fields = {item.name for item in fields(ProviderRequest)}
    if actual_fields != _MODEL_INPUT_REQUEST_FIELDS:
        missing = sorted(actual_fields - _MODEL_INPUT_REQUEST_FIELDS)
        stale = sorted(_MODEL_INPUT_REQUEST_FIELDS - actual_fields)
        raise ModelInputSnapshotError(
            "model-input codec does not cover the current ProviderRequest fields "
            f"(unencoded={missing}, removed={stale})"
        )
    if not isinstance(request.model, str) or not request.model:
        raise TypeError("ProviderRequest.model must be a non-empty string")
    if not isinstance(request.system, list) or not all(
        isinstance(block, SystemBlock) for block in request.system
    ):
        raise TypeError("ProviderRequest.system must be a list of SystemBlock")
    if not isinstance(request.tools, list) or not all(
        isinstance(schema, Mapping) for schema in request.tools
    ):
        raise TypeError("ProviderRequest.tools must be a list of mappings")
    if not isinstance(request.messages, list) or not all(
        isinstance(message, Message) for message in request.messages
    ):
        raise TypeError("ProviderRequest.messages must be a list of Message")

    payload = {
        "model": request.model,
        "system": [
            {"type": block.type, "text": block.text, "cacheable": block.cacheable}
            for block in request.system
        ],
        "tools": request.tools,
        "messages": [message_to_dict(message) for message in request.messages],
        "max_output_tokens": request.max_output_tokens,
        "temperature": request.temperature,
        "stop_sequences": request.stop_sequences,
        "max_retries": request.max_retries,
        "reasoning": request.reasoning,
        "cache_prompt": request.cache_prompt,
        "cache_ttl": request.cache_ttl,
        "thinking": request.thinking,
        "effort": request.effort,
        "output_schema": _output_schema_to_snapshot(request.output_schema),
        "tool_choice": request.tool_choice,
        "stream_partials": request.stream_partials,
    }
    safe = _strict_model_json(payload)
    assert isinstance(safe, dict)
    return safe


def _model_input_integrity_hash(codec_version: int, request_json: str) -> str:
    content = f"{codec_version}\n{request_json}".encode()
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def create_model_input_snapshot(
    run_id: str,
    provider_attempt: int,
    request: ProviderRequest,
    *,
    id: str | None = None,
    created_at: str | None = None,
) -> ModelInputSnapshot:
    """Strictly encode ``request`` while deliberately excluding its live signal."""

    if not isinstance(run_id, str) or not run_id:
        raise ValueError("run_id must be a non-empty string")
    if (
        not isinstance(provider_attempt, int)
        or isinstance(provider_attempt, bool)
        or provider_attempt < 1
    ):
        raise ValueError("provider_attempt must be a positive integer")
    snapshot_id = id or str(uuid4())
    if not isinstance(snapshot_id, str) or not snapshot_id:
        raise ValueError("snapshot id must be a non-empty string")
    payload = _provider_request_payload(request)
    request_json = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    snapshot = ModelInputSnapshot(
        id=snapshot_id,
        run_id=run_id,
        provider_attempt=provider_attempt,
        codec_version=MODEL_INPUT_CODEC_VERSION,
        request_json=request_json,
        integrity_hash=_model_input_integrity_hash(MODEL_INPUT_CODEC_VERSION, request_json),
        created_at=created_at or now_iso(),
    )
    # Decode once at the write boundary so malformed typed fields fail before a
    # provider checkpoint can ever reference this snapshot.
    decode_model_input_snapshot(snapshot)
    return snapshot


def _validated_snapshot_payload(snapshot: ModelInputSnapshot) -> dict[str, Any]:
    if snapshot.codec_version != MODEL_INPUT_CODEC_VERSION:
        raise ModelInputSnapshotError(
            "unsupported model-input snapshot codec version "
            f"{snapshot.codec_version}; expected {MODEL_INPUT_CODEC_VERSION}"
        )
    expected = _model_input_integrity_hash(snapshot.codec_version, snapshot.request_json)
    if snapshot.integrity_hash != expected:
        raise ModelInputSnapshotError(
            f"model-input snapshot {snapshot.id!r} failed its integrity check"
        )
    try:
        raw = json.loads(snapshot.request_json)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ModelInputSnapshotError(
            f"model-input snapshot {snapshot.id!r} contains invalid JSON"
        ) from exc
    if not isinstance(raw, dict):
        raise ModelInputSnapshotError("model-input snapshot request payload must be an object")
    keys = set(raw)
    if keys != _MODEL_INPUT_PAYLOAD_FIELDS:
        raise ModelInputSnapshotError(
            "model-input snapshot request fields do not match codec "
            f"(missing={sorted(_MODEL_INPUT_PAYLOAD_FIELDS - keys)}, "
            f"extra={sorted(keys - _MODEL_INPUT_PAYLOAD_FIELDS)})"
        )
    try:
        safe = _strict_model_json(raw)
    except TypeError as exc:
        raise ModelInputSnapshotError(str(exc)) from exc
    assert isinstance(safe, dict)
    return safe


def _optional_int(raw: Any, name: str) -> int | None:
    if raw is None:
        return None
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise ModelInputSnapshotError(f"{name} must be an integer or null")
    return raw


def _optional_bool(raw: Any, name: str) -> bool | None:
    if raw is None:
        return None
    if not isinstance(raw, bool):
        raise ModelInputSnapshotError(f"{name} must be a boolean or null")
    return raw


def _optional_mapping(raw: Any, name: str) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ModelInputSnapshotError(f"{name} must be an object or null")
    return dict(raw)


def _decode_output_schema(raw: Any) -> OutputSchema | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != {"name", "schema", "strict", "description"}:
        raise ModelInputSnapshotError("output_schema has an invalid shape")
    name = raw["name"]
    schema = raw["schema"]
    strict = raw["strict"]
    description = raw["description"]
    if not isinstance(name, str) or not name:
        raise ModelInputSnapshotError("output_schema.name must be a non-empty string")
    if not isinstance(schema, dict):
        raise ModelInputSnapshotError("output_schema.schema must be an object")
    if not isinstance(strict, bool):
        raise ModelInputSnapshotError("output_schema.strict must be a boolean")
    if description is not None and not isinstance(description, str):
        raise ModelInputSnapshotError("output_schema.description must be a string or null")
    return OutputSchema(name=name, schema=dict(schema), strict=strict, description=description)


def decode_model_input_snapshot(
    snapshot: ModelInputSnapshot,
    *,
    signal: Any = None,
) -> ProviderRequest:
    """Verify and decode a snapshot, rebinding only the caller's live signal."""

    raw = _validated_snapshot_payload(snapshot)
    model = raw["model"]
    if not isinstance(model, str) or not model:
        raise ModelInputSnapshotError("model must be a non-empty string")

    system_raw = raw["system"]
    if not isinstance(system_raw, list):
        raise ModelInputSnapshotError("system must be an array")
    system: list[SystemBlock] = []
    for item in system_raw:
        if not isinstance(item, dict) or set(item) != {"type", "text", "cacheable"}:
            raise ModelInputSnapshotError("system block has an invalid shape")
        if item["type"] != "text" or not isinstance(item["text"], str):
            raise ModelInputSnapshotError("system block must contain text")
        if not isinstance(item["cacheable"], bool):
            raise ModelInputSnapshotError("system block cacheable must be a boolean")
        system.append(SystemBlock(text=item["text"], cacheable=item["cacheable"]))

    tools_raw = raw["tools"]
    if not isinstance(tools_raw, list) or not all(isinstance(item, dict) for item in tools_raw):
        raise ModelInputSnapshotError("tools must be an array of objects")
    tools = [dict(item) for item in tools_raw]

    messages_raw = raw["messages"]
    if not isinstance(messages_raw, list):
        raise ModelInputSnapshotError("messages must be an array")
    messages: list[Message] = []
    try:
        for item in messages_raw:
            if not isinstance(item, dict):
                raise ModelInputSnapshotError("message must be an object")
            if item.get("role") not in ("user", "assistant"):
                raise ModelInputSnapshotError("message role must be user or assistant")
            if not isinstance(item.get("content"), list):
                raise ModelInputSnapshotError("message content must be an array")
            metadata = item.get("provider_metadata")
            if metadata is not None and not isinstance(metadata, dict):
                raise ModelInputSnapshotError("message provider_metadata must be an object or null")
            messages.append(message_from_dict(item))
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ModelInputSnapshotError):
            raise
        raise ModelInputSnapshotError("message content block has an invalid shape") from exc

    max_output_tokens = _optional_int(raw["max_output_tokens"], "max_output_tokens")
    temperature_raw = raw["temperature"]
    if temperature_raw is not None and (
        not isinstance(temperature_raw, int | float) or isinstance(temperature_raw, bool)
    ):
        raise ModelInputSnapshotError("temperature must be a number or null")
    # JSON preserves ``0`` versus ``0.0``; retain the decoded numeric object so
    # even callers that supplied an int at runtime get the same provider input.
    temperature = cast(float | None, temperature_raw)
    stop_raw = raw["stop_sequences"]
    if stop_raw is not None and (
        not isinstance(stop_raw, list) or not all(isinstance(item, str) for item in stop_raw)
    ):
        raise ModelInputSnapshotError("stop_sequences must be an array of strings or null")
    stop_sequences = list(stop_raw) if isinstance(stop_raw, list) else None
    max_retries = raw["max_retries"]
    if not isinstance(max_retries, int) or isinstance(max_retries, bool):
        raise ModelInputSnapshotError("max_retries must be an integer")

    cache_ttl = raw["cache_ttl"]
    if cache_ttl not in (None, "5m", "1h"):
        raise ModelInputSnapshotError("cache_ttl has an invalid value")
    effort = raw["effort"]
    if effort not in (None, "low", "medium", "high", "xhigh", "max"):
        raise ModelInputSnapshotError("effort has an invalid value")
    tool_choice_raw = raw["tool_choice"]
    if isinstance(tool_choice_raw, str):
        if tool_choice_raw not in ("auto", "none", "required"):
            raise ModelInputSnapshotError("tool_choice has an invalid string value")
        tool_choice: Any = tool_choice_raw
    elif isinstance(tool_choice_raw, dict):
        if not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in tool_choice_raw.items()
        ):
            raise ModelInputSnapshotError("tool_choice object must contain string values")
        tool_choice = dict(tool_choice_raw)
    elif tool_choice_raw is None:
        tool_choice = None
    else:
        raise ModelInputSnapshotError("tool_choice must be a string, object, or null")

    return ProviderRequest(
        model=model,
        system=system,
        tools=tools,
        messages=messages,
        signal=signal,
        max_output_tokens=max_output_tokens,
        temperature=temperature,
        stop_sequences=stop_sequences,
        max_retries=max_retries,
        reasoning=_optional_mapping(raw["reasoning"], "reasoning"),
        cache_prompt=_optional_bool(raw["cache_prompt"], "cache_prompt"),
        cache_ttl=cast(Any, cache_ttl),
        thinking=_optional_mapping(raw["thinking"], "thinking"),
        effort=cast(Any, effort),
        output_schema=_decode_output_schema(raw["output_schema"]),
        tool_choice=cast(Any, tool_choice),
        stream_partials=_optional_bool(raw["stream_partials"], "stream_partials"),
    )


def _run_contract_fingerprint(schema_version: int, payload: Mapping[str, Any]) -> str:
    canonical = canonical_json({"schema_version": schema_version, "payload": payload})
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _budget_contract(budget: Any) -> dict[str, Any] | None:
    if budget is None:
        return None
    if isinstance(budget, Mapping):
        return _json_safe(budget, strict=True)
    # Mutable spend counters are checkpoint state, not part of the execution
    # policy.  Keeping only limits avoids a legitimate resume mismatching after
    # the original process charged the shared budget.
    names = ("max_tokens", "max_cost_usd", "warn_ratio")
    if any(hasattr(budget, name) for name in names):
        return {name: _json_safe(getattr(budget, name, None), strict=True) for name in names}
    raise TypeError("budget must be a mapping or expose max_tokens/max_cost_usd/warn_ratio")


def build_run_contract(
    *,
    primary_model: str,
    fallback_models: Sequence[str] | None = None,
    system_blocks: Sequence[Any] | None = None,
    tool_schemas: Sequence[Mapping[str, Any]] | None = None,
    input_payload: Mapping[str, Any] | None = None,
    output_schema: Any = None,
    final_tool_name: str | None = None,
    run_options: Mapping[str, Any] | None = None,
    budget: Any = None,
    policies: Mapping[str, Any] | None = None,
) -> RunContract:
    """Build the normalized contract to persist when a durable run starts.

    Callers should pass the *resolved* values (after Agent/RunOptions defaults),
    and provider-facing tool schemas rather than tool instances.  Tool order is
    retained because it can affect provider behavior and prompt caching.
    """

    if not isinstance(primary_model, str) or not primary_model:
        raise ValueError("primary_model must be a non-empty string")
    fallback_chain = [str(model) for model in (fallback_models or ())]
    if any(not model for model in fallback_chain):
        raise ValueError("fallback_models cannot contain an empty model id")
    tools = [_json_safe(schema, strict=True) for schema in (tool_schemas or ())]
    if any(not isinstance(schema, dict) for schema in tools):
        raise TypeError("every tool schema must be a mapping")

    payload: dict[str, Any] = {
        "models": {"primary": primary_model, "fallbacks": fallback_chain},
        "system": _json_safe(system_blocks or (), strict=True),
        "tools": tools,
        "input": _json_safe(input_payload or {}, strict=True),
        "output": {
            "schema": _json_safe(output_schema, strict=True),
            "final_tool_name": final_tool_name,
        },
        "run_options": _json_safe(run_options or {}, strict=True),
        "budget": _budget_contract(budget),
        "policies": _json_safe(policies or {}, strict=True),
    }
    fingerprint = _run_contract_fingerprint(RUN_CONTRACT_SCHEMA_VERSION, payload)
    return RunContract(
        schema_version=RUN_CONTRACT_SCHEMA_VERSION,
        payload=payload,
        fingerprint=fingerprint,
    )


def run_contract_to_dict(contract: RunContract) -> dict[str, Any]:
    """Encode a run contract with an integrity-checkable fingerprint."""

    payload = _json_safe(contract.payload, strict=True)
    return {
        "schema_version": contract.schema_version,
        "fingerprint": _run_contract_fingerprint(contract.schema_version, payload),
        "payload": payload,
    }


def run_contract_from_dict(raw: Mapping[str, Any]) -> RunContract:
    """Decode old or future contract envelopes without rejecting extra keys."""

    version_raw = raw.get("schema_version", 1)
    if not isinstance(version_raw, int) or isinstance(version_raw, bool) or version_raw < 1:
        raise ValueError("run contract schema_version must be a positive integer")
    payload_raw = raw.get("payload")
    if not isinstance(payload_raw, Mapping):
        raise ValueError("run contract payload must be a mapping")
    payload = _json_safe(payload_raw, strict=True)
    assert isinstance(payload, dict)
    computed = _run_contract_fingerprint(version_raw, payload)
    declared = raw.get("fingerprint")
    # Missing fingerprints are accepted for early/pre-release contract rows.
    # A present but invalid value is retained so compatibility comparison can
    # report corruption instead of silently blessing it.
    fingerprint = declared if isinstance(declared, str) and declared else computed
    return RunContract(schema_version=version_raw, payload=payload, fingerprint=fingerprint)


def run_meta_with_contract(
    contract: RunContract,
    meta: Mapping[str, Any] | None = None,
) -> dict[str, object]:
    """Return JSON-safe run metadata carrying ``contract`` without mutation."""

    safe = _json_safe(meta or {}, strict=False)
    assert isinstance(safe, dict)
    safe[RUN_CONTRACT_META_KEY] = run_contract_to_dict(contract)
    return safe


def run_contract_from_meta(meta: Mapping[str, Any]) -> RunContract | None:
    """Load a persisted contract; ``None`` means a genuinely legacy run."""

    if RUN_CONTRACT_META_KEY not in meta:
        return None
    raw = meta[RUN_CONTRACT_META_KEY]
    if not isinstance(raw, Mapping):
        raise ValueError("persisted run_contract metadata must be a mapping")
    return run_contract_from_dict(raw)


def _contract_differences(
    stored: Any,
    requested: Any,
    path: str = "$",
) -> list[RunContractDifference]:
    if isinstance(stored, dict) and isinstance(requested, dict):
        out: list[RunContractDifference] = []
        for key in sorted(stored.keys() | requested.keys()):
            child_path = f"{path}.{key}"
            if key not in stored:
                out.append(RunContractDifference(child_path, None, requested[key]))
            elif key not in requested:
                out.append(RunContractDifference(child_path, stored[key], None))
            else:
                out.extend(_contract_differences(stored[key], requested[key], child_path))
        return out
    if isinstance(stored, list) and isinstance(requested, list):
        out = []
        for index in range(max(len(stored), len(requested))):
            child_path = f"{path}[{index}]"
            if index >= len(stored):
                out.append(RunContractDifference(child_path, None, requested[index]))
            elif index >= len(requested):
                out.append(RunContractDifference(child_path, stored[index], None))
            else:
                out.extend(_contract_differences(stored[index], requested[index], child_path))
        return out
    if stored != requested or type(stored) is not type(requested):
        return [RunContractDifference(path, stored, requested)]
    return []


def compare_run_contract(
    stored: RunContract | None,
    requested: RunContract,
    *,
    allow_legacy: bool = False,
) -> RunContractComparison:
    """Compare persisted and requested inputs, failing closed for legacy runs.

    Hosts migrating pre-contract records may opt in to ``allow_legacy``.  That
    flag is intentionally explicit: without the original execution inputs,
    compatibility cannot be proven.
    """

    if stored is None:
        return RunContractComparison(
            compatible=allow_legacy,
            legacy=True,
            stored_fingerprint=None,
            requested_fingerprint=requested.fingerprint,
        )
    stored_computed = _run_contract_fingerprint(stored.schema_version, stored.payload)
    requested_computed = _run_contract_fingerprint(requested.schema_version, requested.payload)
    differences: list[RunContractDifference] = []
    if stored.fingerprint != stored_computed:
        differences.append(
            RunContractDifference("$.fingerprint", stored.fingerprint, stored_computed)
        )
    if stored.schema_version != requested.schema_version:
        differences.append(
            RunContractDifference(
                "$.schema_version", stored.schema_version, requested.schema_version
            )
        )
    differences.extend(_contract_differences(stored.payload, requested.payload, "$.payload"))
    compatible = not differences and stored_computed == requested_computed
    return RunContractComparison(
        compatible=compatible,
        legacy=False,
        stored_fingerprint=stored.fingerprint,
        requested_fingerprint=requested.fingerprint,
        differences=tuple(differences),
    )


def ensure_run_contract_compatible(
    stored: RunContract | None,
    requested: RunContract,
    *,
    allow_legacy: bool = False,
) -> RunContractComparison:
    """Return comparison or raise :class:`RunContractMismatchError`."""

    comparison = compare_run_contract(stored, requested, allow_legacy=allow_legacy)
    if not comparison.compatible:
        raise RunContractMismatchError(comparison)
    return comparison


def checkpoint_to_dict(checkpoint: RunCheckpoint) -> dict[str, Any]:
    data = {
        "schema_version": SCHEMA_VERSION,
        "phase": checkpoint.phase,
        "prompt": checkpoint.prompt,
        "turn_index": checkpoint.turn_index,
        "total_usage": usage_to_dict(checkpoint.total_usage),
        "assistant_message": (
            message_to_dict(checkpoint.assistant_message)
            if checkpoint.assistant_message is not None
            else None
        ),
        "pending_tool_blocks": [block_to_dict(block) for block in checkpoint.pending_tool_blocks],
        "completed_tool_results": {
            key: block_to_dict(block) for key, block in checkpoint.completed_tool_results.items()
        },
        "force_final_pending": checkpoint.force_final_pending,
        "loop_guard_state": checkpoint.loop_guard_state,
        "pending_skill_overlay": checkpoint.pending_skill_overlay,
        "current_turn_allowed_tools": checkpoint.current_turn_allowed_tools,
        "assistant_stop_reason": checkpoint.assistant_stop_reason,
        "permission_decisions": checkpoint.permission_decisions,
        "background_workers": checkpoint.background_workers,
        "truncation_attempts": checkpoint.truncation_attempts,
        "truncation_prefix": checkpoint.truncation_prefix,
        "pending_truncation_feedback": checkpoint.pending_truncation_feedback,
        "tool_batch_event_after_seq": checkpoint.tool_batch_event_after_seq,
        "pending_alignment": checkpoint.pending_alignment,
    }
    # Additive extensions must cost nothing when unused: preserving the legacy
    # wire shape for an empty namespace keeps harness-off checkpoint payloads
    # byte-identical.  Readers still default a missing key to ``{}``.
    if checkpoint.extension_state:
        data["extension_state"] = checkpoint.extension_state
    if checkpoint.provider_attempt:
        data["provider_attempt"] = checkpoint.provider_attempt
    if checkpoint.model_input_snapshot_id is not None:
        data["model_input_snapshot_id"] = checkpoint.model_input_snapshot_id
    safe = _json_safe(data, strict=False)
    assert isinstance(safe, dict)
    return safe


def _dict_or_none(value: Any) -> dict[str, object] | None:
    return {str(key): item for key, item in value.items()} if isinstance(value, dict) else None


def _pending_alignment_from_raw(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    entries: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        prompt = str(item.get("prompt", "") or "")
        if not prompt:
            continue
        images_raw = item.get("images")
        images = (
            [dict(image) for image in images_raw if isinstance(image, dict)]
            if isinstance(images_raw, list)
            else None
        )
        entries.append({"prompt": prompt, "images": images})
    return entries


def _extension_state_from_raw(value: Any) -> dict[str, Any]:
    """Restore the extension namespace without making legacy reads brittle.

    Checkpoints arrive from JSON-backed stores, but this parser is also public
    and is used by host stores.  Treat a missing or malformed root as no
    extension state; per-extension payload validation belongs to the extension
    that owns the key when it restores its state.
    """
    if not isinstance(value, dict):
        return {}
    return {key: item for key, item in value.items() if isinstance(key, str)}


def checkpoint_from_dict(raw: dict[str, Any]) -> RunCheckpoint:
    assistant_raw = raw.get("assistant_message")
    pending_blocks: list[ToolUseBlock] = []
    for item in raw.get("pending_tool_blocks", []):
        if isinstance(item, dict):
            block = block_from_dict(item)
            if isinstance(block, ToolUseBlock):
                pending_blocks.append(block)

    completed: dict[str, ToolResultBlock] = {}
    completed_raw = raw.get("completed_tool_results", {})
    if isinstance(completed_raw, dict):
        for key, item in completed_raw.items():
            if isinstance(item, dict):
                block = block_from_dict(item)
                if isinstance(block, ToolResultBlock):
                    completed[str(key)] = block

    return RunCheckpoint(
        phase=raw.get("phase", "started"),
        prompt=str(raw.get("prompt", "")),
        turn_index=int(raw.get("turn_index", 0) or 0),
        total_usage=usage_from_dict(dict(raw.get("total_usage", {}))),
        assistant_message=(
            message_from_dict(dict(assistant_raw)) if isinstance(assistant_raw, dict) else None
        ),
        pending_tool_blocks=pending_blocks,
        completed_tool_results=completed,
        force_final_pending=bool(raw.get("force_final_pending", False)),
        loop_guard_state=_dict_or_none(raw.get("loop_guard_state")),
        pending_skill_overlay=_dict_or_none(raw.get("pending_skill_overlay")),
        current_turn_allowed_tools=(
            [str(t) for t in raw.get("current_turn_allowed_tools", [])]
            if isinstance(raw.get("current_turn_allowed_tools"), list)
            else None
        ),
        assistant_stop_reason=(
            str(raw.get("assistant_stop_reason"))
            if isinstance(raw.get("assistant_stop_reason"), str)
            else None
        ),
        permission_decisions=(
            {str(k): dict(v) for k, v in raw.get("permission_decisions", {}).items()}
            if isinstance(raw.get("permission_decisions"), dict)
            else {}
        ),
        background_workers=(
            {str(k): dict(v) for k, v in raw.get("background_workers", {}).items()}
            if isinstance(raw.get("background_workers"), dict)
            else {}
        ),
        truncation_attempts=max(0, int(raw.get("truncation_attempts", 0) or 0)),
        truncation_prefix=(
            str(raw.get("truncation_prefix"))
            if isinstance(raw.get("truncation_prefix"), str)
            else ""
        ),
        pending_truncation_feedback=(
            str(raw.get("pending_truncation_feedback"))
            if isinstance(raw.get("pending_truncation_feedback"), str)
            else None
        ),
        tool_batch_event_after_seq=(
            int(raw["tool_batch_event_after_seq"])
            if isinstance(raw.get("tool_batch_event_after_seq"), int)
            else None
        ),
        pending_alignment=_pending_alignment_from_raw(raw.get("pending_alignment")),
        extension_state=_extension_state_from_raw(raw.get("extension_state")),
        provider_attempt=(
            max(0, raw["provider_attempt"])
            if isinstance(raw.get("provider_attempt"), int)
            and not isinstance(raw.get("provider_attempt"), bool)
            else 0
        ),
        model_input_snapshot_id=(
            raw["model_input_snapshot_id"]
            if isinstance(raw.get("model_input_snapshot_id"), str)
            and raw["model_input_snapshot_id"]
            else None
        ),
    )


def _safe_meta(meta: Any) -> dict[str, object]:
    safe = _json_safe(meta, strict=False)
    return safe if isinstance(safe, dict) else {}


def _append_run_error(meta: dict[str, object], error: dict[str, object]) -> None:
    """Append an error without discarding malformed or legacy metadata."""

    existing = meta.get("errors")
    if isinstance(existing, list):
        errors = existing
    else:
        errors = [] if existing is None else [existing]
        meta["errors"] = errors
    errors.append(_json_safe(error, strict=False))


def _copy_checkpoint(checkpoint: RunCheckpoint | None) -> RunCheckpoint | None:
    if checkpoint is None:
        return None
    return checkpoint_from_dict(checkpoint_to_dict(checkpoint))


def _copy_record(record: RunRecord) -> RunRecord:
    return RunRecord(
        id=record.id,
        session_id=record.session_id,
        status=record.status,
        created_at=record.created_at,
        updated_at=record.updated_at,
        checkpoint=_copy_checkpoint(record.checkpoint),
        meta=_safe_meta(record.meta),
    )


def _copy_event(event: Event) -> Event:
    raw = _json_safe(event_to_dict(event), strict=False)
    assert isinstance(raw, dict)
    return event_from_dict(raw)


def _validate_model_input_snapshot(snapshot: ModelInputSnapshot) -> ModelInputSnapshot:
    if not snapshot.id or not snapshot.run_id:
        raise ModelInputSnapshotError("model-input snapshot id and run_id must be non-empty")
    if (
        not isinstance(snapshot.provider_attempt, int)
        or isinstance(snapshot.provider_attempt, bool)
        or snapshot.provider_attempt < 1
    ):
        raise ModelInputSnapshotError("model-input snapshot provider_attempt must be positive")
    if not isinstance(snapshot.created_at, str) or not snapshot.created_at:
        raise ModelInputSnapshotError("model-input snapshot created_at must be non-empty")
    _validated_snapshot_payload(snapshot)
    return snapshot


class InMemoryRunStore:
    def __init__(self) -> None:
        self._runs: dict[str, RunRecord] = {}
        self._events: dict[str, list[StoredRunEvent]] = {}
        self._model_input_snapshots: dict[str, ModelInputSnapshot] = {}

    async def create_run(
        self,
        session_id: str,
        *,
        id: str | None = None,
        meta: dict[str, object] | None = None,
    ) -> RunRecord:
        rid = id or str(uuid4())
        existing = self._runs.get(rid)
        if existing is not None:
            return _copy_record(existing)
        ts = now_iso()
        record = RunRecord(
            id=rid,
            session_id=session_id,
            status="running",
            created_at=ts,
            updated_at=ts,
            meta=_safe_meta(meta),
        )
        self._runs[rid] = record
        self._events[rid] = []
        return _copy_record(record)

    async def load_run(self, run_id: str) -> RunRecord | None:
        record = self._runs.get(run_id)
        return _copy_record(record) if record is not None else None

    async def save_checkpoint(
        self,
        run_id: str,
        checkpoint: RunCheckpoint,
        *,
        status: str = "running",
    ) -> RunRecord:
        rec = self._runs[run_id]
        rec.checkpoint = _copy_checkpoint(checkpoint)
        rec.status = status  # type: ignore[assignment]
        rec.updated_at = now_iso()
        return _copy_record(rec)

    async def append_event(self, run_id: str, event: Event) -> int:
        if run_id not in self._runs:
            raise KeyError(f"run not found: {run_id}")
        bucket = self._events.setdefault(run_id, [])
        seq = len(bucket) + 1
        bucket.append(StoredRunEvent(seq=seq, appended_at=now_iso(), event=_copy_event(event)))
        return seq

    async def append_events(self, run_id: str, events: list[Event]) -> list[int]:
        # Delegate to append_event so subclass overrides (test doubles that drop a
        # start event) are honored through the batch path.
        return [await self.append_event(run_id, event) for event in events]

    async def load_events(self, run_id: str, *, after_seq: int = 0) -> list[StoredRunEvent]:
        return [
            StoredRunEvent(seq=row.seq, appended_at=row.appended_at, event=_copy_event(row.event))
            for row in self._events.get(run_id, [])
            if row.seq > after_seq
        ]

    async def save(
        self,
        run_id: str,
        provider_attempt: int,
        request: ProviderRequest,
    ) -> ModelInputSnapshot:
        if run_id not in self._runs:
            raise KeyError(f"run not found: {run_id}")
        snapshot = create_model_input_snapshot(run_id, provider_attempt, request)
        self._model_input_snapshots[snapshot.id] = snapshot
        return snapshot

    async def load(self, snapshot_id: str) -> ModelInputSnapshot | None:
        snapshot = self._model_input_snapshots.get(snapshot_id)
        return _validate_model_input_snapshot(snapshot) if snapshot is not None else None

    async def delete(self, snapshot_id: str) -> None:
        self._model_input_snapshots.pop(snapshot_id, None)

    async def prune(self, run_id: str, keep_ids: Sequence[str] = ()) -> int:
        keep = set(keep_ids)
        doomed = [
            snapshot_id
            for snapshot_id, snapshot in self._model_input_snapshots.items()
            if snapshot.run_id == run_id and snapshot_id not in keep
        ]
        for snapshot_id in doomed:
            del self._model_input_snapshots[snapshot_id]
        return len(doomed)

    async def mark_completed(self, run_id: str, checkpoint: RunCheckpoint) -> RunRecord:
        checkpoint.phase = "completed"
        return await self.save_checkpoint(run_id, checkpoint, status="completed")

    async def mark_failed(
        self,
        run_id: str,
        checkpoint: RunCheckpoint | None = None,
        error: dict[str, object] | None = None,
    ) -> RunRecord:
        rec = self._runs[run_id]
        if checkpoint is not None:
            checkpoint.phase = "failed"
            rec.checkpoint = _copy_checkpoint(checkpoint)
        if error is not None:
            _append_run_error(rec.meta, error)
        rec.status = "failed"
        rec.updated_at = now_iso()
        return _copy_record(rec)

    async def close(self) -> None:
        return None


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        create table if not exists runs (
          id text primary key,
          session_id text not null,
          status text not null,
          created_at text not null,
          updated_at text not null,
          checkpoint text,
          meta text not null
        );
        create table if not exists run_events (
          run_id text not null,
          seq integer not null,
          appended_at text not null,
          event text not null,
          primary key (run_id, seq)
        );
        create table if not exists model_input_snapshots (
          id text primary key,
          run_id text not null,
          provider_attempt integer not null,
          codec_version integer not null,
          request_json text not null,
          integrity_hash text not null,
          created_at text not null
        );
        create index if not exists model_input_snapshots_run_id_idx
          on model_input_snapshots (run_id);
        """
    )


def _record(row: object) -> RunRecord:
    checkpoint_raw = row[5]  # type: ignore[index]
    return RunRecord(
        id=row[0],  # type: ignore[index]
        session_id=row[1],  # type: ignore[index]
        status=row[2],  # type: ignore[index]
        created_at=row[3],  # type: ignore[index]
        updated_at=row[4],  # type: ignore[index]
        checkpoint=(checkpoint_from_dict(json.loads(checkpoint_raw)) if checkpoint_raw else None),
        meta=_safe_meta(json.loads(row[6] or "{}")),  # type: ignore[index]
    )


class SqliteRunStore:
    def __init__(self, path: str | Path = ".linch/runs.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._exec = SqliteExecutor(self.path, init=_init_schema, wal=True)

    async def create_run(
        self,
        session_id: str,
        *,
        id: str | None = None,
        meta: dict[str, object] | None = None,
    ) -> RunRecord:
        return await self._exec.run(lambda c: _create_run(c, session_id, id, meta or {}))

    async def load_run(self, run_id: str) -> RunRecord | None:
        return await self._exec.run(lambda c: _load_run(c, run_id))

    async def save_checkpoint(
        self,
        run_id: str,
        checkpoint: RunCheckpoint,
        *,
        status: str = "running",
    ) -> RunRecord:
        return await self._exec.run(lambda c: _save_checkpoint(c, run_id, checkpoint, status))

    async def append_event(self, run_id: str, event: Event) -> int:
        return await self._exec.run(lambda c: _append_event(c, run_id, event))

    async def append_events(self, run_id: str, events: list[Event]) -> list[int]:
        if not events:
            return []
        return await self._exec.run(lambda c: _append_events(c, run_id, events))

    async def load_events(self, run_id: str, *, after_seq: int = 0) -> list[StoredRunEvent]:
        return await self._exec.run(lambda c: _load_events(c, run_id, after_seq))

    async def save(
        self,
        run_id: str,
        provider_attempt: int,
        request: ProviderRequest,
    ) -> ModelInputSnapshot:
        snapshot = create_model_input_snapshot(run_id, provider_attempt, request)
        return await self._exec.run(lambda c: _save_model_input_snapshot(c, snapshot))

    async def load(self, snapshot_id: str) -> ModelInputSnapshot | None:
        return await self._exec.run(lambda c: _load_model_input_snapshot(c, snapshot_id))

    async def delete(self, snapshot_id: str) -> None:
        await self._exec.run(lambda c: _delete_model_input_snapshot(c, snapshot_id))

    async def prune(self, run_id: str, keep_ids: Sequence[str] = ()) -> int:
        ids = tuple(str(snapshot_id) for snapshot_id in keep_ids)
        return await self._exec.run(lambda c: _prune_model_input_snapshots(c, run_id, ids))

    async def mark_completed(self, run_id: str, checkpoint: RunCheckpoint) -> RunRecord:
        checkpoint.phase = "completed"
        return await self.save_checkpoint(run_id, checkpoint, status="completed")

    async def mark_failed(
        self,
        run_id: str,
        checkpoint: RunCheckpoint | None = None,
        error: dict[str, object] | None = None,
    ) -> RunRecord:
        return await self._exec.run(lambda c: _mark_failed(c, run_id, checkpoint, error))

    async def close(self) -> None:
        await self._exec.close()

    def __enter__(self) -> SqliteRunStore:
        return self

    def __exit__(self, *_: object) -> None:
        self._exec.close_sync()

    async def __aenter__(self) -> SqliteRunStore:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()


def _create_run(
    conn: sqlite3.Connection,
    session_id: str,
    id: str | None,
    meta: dict[str, object],
) -> RunRecord:
    safe_meta = _safe_meta(meta)
    rid = id or str(uuid4())
    row = conn.execute(
        "select id, session_id, status, created_at, updated_at, checkpoint, meta "
        "from runs where id = ?",
        (rid,),
    ).fetchone()
    if row:
        return _record(row)
    ts = now_iso()
    conn.execute(
        "insert into runs (id, session_id, status, created_at, updated_at, checkpoint, meta) "
        "values (?, ?, 'running', ?, ?, null, ?)",
        (rid, session_id, ts, ts, json.dumps(safe_meta, allow_nan=False)),
    )
    conn.commit()
    return RunRecord(
        id=rid,
        session_id=session_id,
        status="running",
        created_at=ts,
        updated_at=ts,
        meta=safe_meta,
    )


def _load_run(conn: sqlite3.Connection, run_id: str) -> RunRecord | None:
    row = conn.execute(
        "select id, session_id, status, created_at, updated_at, checkpoint, meta "
        "from runs where id = ?",
        (run_id,),
    ).fetchone()
    return _record(row) if row else None


def _save_model_input_snapshot(
    conn: sqlite3.Connection,
    snapshot: ModelInputSnapshot,
) -> ModelInputSnapshot:
    if conn.execute("select 1 from runs where id = ?", (snapshot.run_id,)).fetchone() is None:
        raise KeyError(f"run not found: {snapshot.run_id}")
    conn.execute(
        "insert into model_input_snapshots "
        "(id, run_id, provider_attempt, codec_version, request_json, integrity_hash, created_at) "
        "values (?, ?, ?, ?, ?, ?, ?)",
        (
            snapshot.id,
            snapshot.run_id,
            snapshot.provider_attempt,
            snapshot.codec_version,
            snapshot.request_json,
            snapshot.integrity_hash,
            snapshot.created_at,
        ),
    )
    conn.commit()
    return snapshot


def _load_model_input_snapshot(
    conn: sqlite3.Connection,
    snapshot_id: str,
) -> ModelInputSnapshot | None:
    row = conn.execute(
        "select id, run_id, provider_attempt, codec_version, request_json, integrity_hash, "
        "created_at from model_input_snapshots where id = ?",
        (snapshot_id,),
    ).fetchone()
    if row is None:
        return None
    snapshot = ModelInputSnapshot(
        id=row[0],
        run_id=row[1],
        provider_attempt=row[2],
        codec_version=row[3],
        request_json=row[4],
        integrity_hash=row[5],
        created_at=row[6],
    )
    return _validate_model_input_snapshot(snapshot)


def _delete_model_input_snapshot(conn: sqlite3.Connection, snapshot_id: str) -> None:
    conn.execute("delete from model_input_snapshots where id = ?", (snapshot_id,))
    conn.commit()


def _prune_model_input_snapshots(
    conn: sqlite3.Connection,
    run_id: str,
    keep_ids: tuple[str, ...],
) -> int:
    if keep_ids:
        placeholders = ",".join("?" for _ in keep_ids)
        cursor = conn.execute(
            f"delete from model_input_snapshots where run_id = ? and id not in ({placeholders})",
            (run_id, *keep_ids),
        )
    else:
        cursor = conn.execute("delete from model_input_snapshots where run_id = ?", (run_id,))
    conn.commit()
    return max(0, cursor.rowcount)


def _save_checkpoint(
    conn: sqlite3.Connection,
    run_id: str,
    checkpoint: RunCheckpoint,
    status: str,
) -> RunRecord:
    row = conn.execute(
        "select id, session_id, status, created_at, updated_at, checkpoint, meta "
        "from runs where id = ?",
        (run_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"run not found: {run_id}")
    ts = now_iso()
    checkpoint_data = checkpoint_to_dict(checkpoint)
    conn.execute(
        "update runs set updated_at = ?, status = ?, checkpoint = ? where id = ?",
        (ts, status, json.dumps(checkpoint_data, allow_nan=False), run_id),
    )
    conn.commit()
    return RunRecord(
        id=row[0],
        session_id=row[1],
        status=status,  # type: ignore[arg-type]
        created_at=row[3],
        updated_at=ts,
        checkpoint=checkpoint_from_dict(checkpoint_data),
        meta=_safe_meta(json.loads(row[6] or "{}")),
    )


def _append_event(conn: sqlite3.Connection, run_id: str, event: Event) -> int:
    row = conn.execute("select coalesce(max(seq), 0) from run_events where run_id = ?", (run_id,))
    seq = int(row.fetchone()[0]) + 1
    ts = now_iso()
    conn.execute(
        "insert into run_events (run_id, seq, appended_at, event) values (?, ?, ?, ?)",
        (
            run_id,
            seq,
            ts,
            json.dumps(_json_safe(event_to_dict(event), strict=False), allow_nan=False),
        ),
    )
    conn.commit()
    return seq


def _append_events(conn: sqlite3.Connection, run_id: str, events: list[Event]) -> list[int]:
    # One MAX lookup + executemany + one commit, amortizing the per-event round
    # trip. The executor's locked call rolls back on error, so it is all-or-nothing.
    base = int(
        conn.execute(
            "select coalesce(max(seq), 0) from run_events where run_id = ?", (run_id,)
        ).fetchone()[0]
    )
    ts = now_iso()
    rows = [
        (
            run_id,
            base + i,
            ts,
            json.dumps(_json_safe(event_to_dict(event), strict=False), allow_nan=False),
        )
        for i, event in enumerate(events, start=1)
    ]
    conn.executemany(
        "insert into run_events (run_id, seq, appended_at, event) values (?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return [base + i for i in range(1, len(events) + 1)]


def _load_events(
    conn: sqlite3.Connection,
    run_id: str,
    after_seq: int,
) -> list[StoredRunEvent]:
    rows = conn.execute(
        "select seq, appended_at, event from run_events where run_id = ? and seq > ? order by seq",
        (run_id, after_seq),
    ).fetchall()
    return [
        StoredRunEvent(
            seq=row[0],
            appended_at=row[1],
            event=event_from_dict(json.loads(row[2])),
        )
        for row in rows
    ]


def _mark_failed(
    conn: sqlite3.Connection,
    run_id: str,
    checkpoint: RunCheckpoint | None,
    error: dict[str, object] | None,
) -> RunRecord:
    row = conn.execute(
        "select id, session_id, status, created_at, updated_at, checkpoint, meta "
        "from runs where id = ?",
        (run_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"run not found: {run_id}")
    meta = _safe_meta(json.loads(row[6] or "{}"))
    if error is not None:
        _append_run_error(meta, error)
    ts = now_iso()
    checkpoint_json = row[5]
    checkpoint_data: dict[str, Any] | None = None
    if checkpoint is not None:
        checkpoint.phase = "failed"
        checkpoint_data = checkpoint_to_dict(checkpoint)
        checkpoint_json = json.dumps(checkpoint_data, allow_nan=False)
    elif checkpoint_json:
        checkpoint_data = json.loads(checkpoint_json)
    safe_meta = _safe_meta(meta)
    conn.execute(
        "update runs set updated_at = ?, status = 'failed', checkpoint = ?, meta = ? where id = ?",
        (ts, checkpoint_json, json.dumps(safe_meta, allow_nan=False), run_id),
    )
    conn.commit()
    return RunRecord(
        id=row[0],
        session_id=row[1],
        status="failed",
        created_at=row[3],
        updated_at=ts,
        checkpoint=(checkpoint_from_dict(checkpoint_data) if checkpoint_data is not None else None),
        meta=safe_meta,
    )
