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
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

from .events import Event, event_from_dict, event_to_dict, usage_from_dict, usage_to_dict
from .sessions.memory import now_iso
from .storage._executor import SqliteExecutor
from .types import (
    Message,
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
# reads any version best-effort (unknown future keys are ignored) and
# `load_events` drops events it cannot decode, so a newer store is forward-safe.
SCHEMA_VERSION = 1

# A run contract is versioned independently from the checkpoint wire format.
# Checkpoints describe *where* execution stopped; this contract describes the
# execution inputs that must stay stable when that checkpoint is resumed.
RUN_CONTRACT_SCHEMA_VERSION = 1
RUN_CONTRACT_META_KEY = "run_contract"

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


_JSON_SAFE_MAX_DEPTH = 100


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
    )


def _safe_meta(meta: Any) -> dict[str, object]:
    safe = _json_safe(meta, strict=False)
    return safe if isinstance(safe, dict) else {}


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


class InMemoryRunStore:
    def __init__(self) -> None:
        self._runs: dict[str, RunRecord] = {}
        self._events: dict[str, list[StoredRunEvent]] = {}

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
            rec.meta.setdefault("errors", [])
            errors = rec.meta["errors"]
            if isinstance(errors, list):
                errors.append(_json_safe(error, strict=False))
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
    out: list[StoredRunEvent] = []
    for row in rows:
        try:
            event = event_from_dict(json.loads(row[2]))
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            # Forward-compat: an event written by a newer schema (unknown type or
            # shape) is skipped rather than aborting the whole resume. The
            # checkpoint, not the event log, drives resume.
            continue
        out.append(StoredRunEvent(seq=row[0], appended_at=row[1], event=event))
    return out


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
        errors = meta.setdefault("errors", [])
        if isinstance(errors, list):
            errors.append(_json_safe(error, strict=False))
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
