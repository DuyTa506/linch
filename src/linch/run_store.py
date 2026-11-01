from __future__ import annotations

import base64
import errno
import hashlib
import inspect
import json
import math
import time
from collections.abc import Awaitable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Protocol, TypeVar, cast
from uuid import UUID, uuid4

from ._json import walk_json
from ._telemetry import store_call
from ._wire import WIRE_VERSION, require_wire_version
from .errors import ConfigError
from .events import (
    IGNORABLE_MAX_DEPTH,
    Event,
    _stored_event_from_dict,
    _stored_event_to_dict,
    usage_from_dict,
    usage_to_dict,
)
from .sessions.memory import now_iso
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

# Wire-format version for serialized run state.  Keep this alias for the
# existing top-level ``RUN_SCHEMA_VERSION`` export; the private module owns the
# one actual version constant shared by every persisted envelope.
SCHEMA_VERSION = WIRE_VERSION

# Run contracts share the common wire generation with checkpoints. Checkpoints
# describe where execution stopped; contracts bind the execution inputs that
# must stay stable when that checkpoint is resumed.
RUN_CONTRACT_SCHEMA_VERSION = WIRE_VERSION
RUN_CONTRACT_META_KEY = "run_contract"

# Model input snapshots share the common wire generation. Their exact-input
# integrity checks additionally require a pending provider request to decode
# exactly or fail closed.
MODEL_INPUT_CODEC_VERSION = WIRE_VERSION

# "suspended" is a workflow parked at a wf.interrupt; distinct from
# "waiting_permission", which belongs to the tool-permission flow.
RunStatus = Literal[
    "pending", "running", "waiting_permission", "suspended", "completed", "failed", "aborted"
]
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
    """Capture the durable state needed to resume one run.

    Checkpoints are serialized at phase boundaries and restored before a run
    resumes. Store implementations detach the checkpoint from caller-owned
    objects, while extension state preserves namespaces owned by optional
    runtime features.

    Args:
        phase: Execution phase represented by the checkpoint.
        prompt: User prompt associated with the run.
        turn_index: Current loop turn index.
        total_usage: Cumulative provider usage through this checkpoint.
        assistant_message: Accepted assistant message for the current turn, if any.
        pending_tool_blocks: Tool calls waiting for permission or execution.
        completed_tool_results: Tool results already recovered for the current batch.
        force_final_pending: Whether the loop must take its forced-final path.
        loop_guard_state: Serialized loop-guard state, when enabled.
        pending_skill_overlay: Skill overlay waiting to be applied, if any.
        current_turn_allowed_tools: Tool names allowed for the current turn, if restricted.
        assistant_stop_reason: Provider stop reason for the accepted assistant response.
        permission_decisions: Durable permission decisions keyed by tool call or request.
        background_workers: Durable state for background workers associated with the run.
        truncation_attempts: Number of output-truncation continuation attempts used for the run.
        truncation_prefix: Assistant text accumulated from previous output-truncated responses.
        pending_truncation_feedback: Continuation feedback waiting to be appended before retrying.
        tool_batch_event_after_seq: Event-log cursor immediately before the tool batch.
        pending_alignment: Undrained session-alignment entries to restore on resume.
        extension_state: JSON-safe namespaces owned by optional runtime extensions.
        provider_attempt: 1-based provider dispatch attempt captured for exact replay.
        model_input_snapshot_id: Exact provider-request snapshot referenced by this checkpoint.

    Attributes:
        phase: Execution phase represented by the checkpoint.
        prompt: User prompt associated with the run.
        turn_index: Current loop turn index.
        total_usage: Cumulative provider usage through this checkpoint.
        extension_state: Opaque JSON-safe state preserved for runtime extensions.
        provider_attempt: Provider dispatch attempt associated with an exact snapshot.
        model_input_snapshot_id: Snapshot identifier used to replay a pending provider call.
    """

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
    Tool recovery requires a non-negative integer; non-tool phases may use ``None``."""
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
    independently installed extensions can share one durable checkpoint.
    """
    provider_attempt: int = 0
    """1-based provider dispatch attempt for an exact model-input snapshot.

    Zero means no provider attempt has been captured.
    """
    model_input_snapshot_id: str | None = None
    """Snapshot referenced by a ``provider_pending`` checkpoint, when enabled."""


@dataclass(slots=True)
class RunRecord:
    """Represent the durable identity, status, metadata, and checkpoint of a run.

    Args:
        id: Stable run identifier.
        session_id: Session that owns the run.
        status: Current run lifecycle status.
        created_at: Creation timestamp in the store's ISO representation.
        updated_at: Timestamp of the most recent durable update.
        checkpoint: Latest resumable checkpoint, if one has been written.
        meta: JSON-safe run metadata, including an optional run contract.

    Attributes:
        id: Stable run identifier.
        session_id: Session that owns the run.
        status: Current run lifecycle status.
        created_at: Creation timestamp in the store's ISO representation.
        updated_at: Timestamp of the most recent durable update.
        checkpoint: Latest resumable checkpoint, if one has been written.
        meta: JSON-safe run metadata.
    """

    id: str
    session_id: str
    status: RunStatus
    created_at: str
    updated_at: str
    checkpoint: RunCheckpoint | None = None
    meta: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class StoredRunEvent:
    """Represent one ordered event persisted in a run's event log.

    Args:
        seq: Monotonically increasing, 1-based sequence number within the run.
        appended_at: Timestamp assigned when the event was persisted.
        event: Typed event payload.

    Attributes:
        seq: Monotonically increasing, 1-based sequence number within the run.
        appended_at: Timestamp assigned when the event was persisted.
        event: Typed event payload.
    """

    seq: int
    appended_at: str
    event: Event


@dataclass(frozen=True, slots=True)
class RunLease:
    """A fencing lease held by one worker for one run.

    ``expires_at`` is a Unix timestamp.  Epochs are monotonically increasing
    per run and are the fencing token: a holder whose epoch is no longer the
    stored epoch cannot mutate the run.

    Args:
        run_id: Run whose writes this lease may fence.
        owner: Unique worker or process identifier holding the lease.
        epoch: Monotonically increasing fencing token for the run.
        expires_at: Lease expiry as a Unix timestamp.
        ttl_s: Renewal duration carried by this immutable token.

    Attributes:
        run_id: Run whose writes this lease may fence.
        owner: Unique worker or process identifier holding the lease.
        epoch: Monotonically increasing fencing token for the run.
        expires_at: Lease expiry as a Unix timestamp.
        ttl_s: Renewal duration carried by this immutable token.
    """

    run_id: str
    owner: str
    epoch: int
    expires_at: float
    ttl_s: float


class RunLeaseConflictError(RuntimeError):
    """Raised when a run has an unexpired lease owned by another worker."""


class LeaseLostError(RuntimeError):
    """Raised when a holder attempts a write after its fencing epoch changed."""


class _StoreUnavailableError(LeaseLostError):
    """Transient store failure leaves the run retryable."""


def _is_transient_store_error(error: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (ConnectionError, TimeoutError)):
            return True
        if isinstance(current, OSError) and current.errno in (
            errno.ENETDOWN,
            errno.ENETUNREACH,
            errno.EHOSTDOWN,
            errno.EHOSTUNREACH,
        ):
            return True
        if (
            type(current).__module__.startswith("asyncpg.")
            and type(current).__name__ == "InterfaceError"
            and str(current) in ("connection is closed", "pool is closed", "pool is closing")
        ):
            return True
        if any(
            base.__module__.startswith("asyncpg.")
            and base.__name__
            in (
                "PostgresConnectionError",
                "AdminShutdownError",
                "CrashShutdownError",
                "CannotConnectNowError",
                "TooManyConnectionsError",
            )
            for base in type(current).__mro__
        ):
            return True
        current = current.__cause__
    return False


_T = TypeVar("_T")


async def durable_call(session: Any, op: str, awaitable: Awaitable[_T]) -> _T:
    """Measure a durable operation and preserve active runs on transient failure.

    Args:
        session: Session whose configured run store and active run govern recovery.
        op: Measurement label; it does not affect failure classification.
        awaitable: Durable operation to execute, excluding run creation.

    Returns:
        The operation's result without modification.

    Raises:
        _StoreUnavailableError: A transient store failure interrupts an active durable run.
    """
    try:
        return await store_call(session, op, awaitable)
    except Exception as exc:
        if (
            session is not None
            and getattr(session.agent, "run_store", None) is not None
            and getattr(session, "active_run_id", None) is not None
        ):
            if not isinstance(exc, _StoreUnavailableError) and _is_transient_store_error(exc):
                raise _StoreUnavailableError(f"transient store failure during {op}") from exc
        raise


class RunLeaseStore(Protocol):
    """Optional capability for claiming and fencing ownership of a run.

    A lease carries a monotonically increasing epoch. Implementations reject a
    write made with a stale epoch, while stores and runs that do not opt into
    leases retain their normal single-process behavior.
    """

    async def claim(self, run_id: str, owner: str, ttl_s: float) -> RunLease:
        """Claim a run for an owner until the requested TTL expires.

        Args:
            run_id: Existing run to claim.
            owner: Unique identifier for the worker or process taking ownership.
            ttl_s: Positive finite lease duration in seconds.

        Returns:
            The immutable lease containing the current fencing epoch and expiry.

        Raises:
            KeyError: If ``run_id`` does not identify a run.
            RunLeaseConflictError: If another owner holds a live lease.
            ValueError: If ``ttl_s`` is not positive and finite.
        """

        ...

    async def renew(self, lease: RunLease) -> RunLease:
        """Extend a currently held lease without changing its epoch.

        Args:
            lease: Immutable owner, epoch, and renewal-duration token.

        Returns:
            A lease with the same owner and epoch and a later expiry.

        Raises:
            LeaseLostError: If the lease is stale, expired, or no longer held.
        """

        ...

    async def release(self, lease: RunLease) -> None:
        """Release a matching lease and advance the run's fencing epoch.

        Args:
            lease: Lease whose ownership should be relinquished.

        Raises:
            LeaseLostError: If another holder has already changed the epoch.
        """

        ...


@dataclass(frozen=True, slots=True)
class ModelInputSnapshot:
    """Immutable encoded provider request captured immediately before dispatch.

    ``request_json`` is canonical JSON rather than a mutable ``ProviderRequest``
    graph.  Use :func:`decode_model_input_snapshot` to integrity-check it and
    recreate a request with the current process's live abort signal.

    Args:
        id: Stable snapshot identifier.
        run_id: Run associated with the provider request.
        provider_attempt: 1-based provider dispatch attempt.
        codec_version: Wire version used to encode the request.
        request_json: Canonical JSON request payload without the live signal.
        integrity_hash: Hash covering the codec version and request payload.
        created_at: Snapshot creation timestamp.

    Attributes:
        id: Stable snapshot identifier.
        run_id: Run associated with the provider request.
        provider_attempt: 1-based provider dispatch attempt.
        codec_version: Wire version used to encode the request.
        request_json: Canonical JSON request payload without the live signal.
        integrity_hash: Hash covering the codec version and request payload.
        created_at: Snapshot creation timestamp.
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

    Args:
        schema_version: Wire version defining the contract envelope semantics.
        payload: Canonical JSON-safe execution inputs.
        fingerprint: Digest of the schema version and payload.

    Attributes:
        schema_version: Wire version defining the contract envelope semantics.
        payload: Canonical JSON-safe execution inputs.
        fingerprint: Digest used to detect changed resume inputs.
    """

    schema_version: int
    payload: dict[str, Any]
    fingerprint: str


@dataclass(frozen=True, slots=True)
class RunContractDifference:
    """One actionable difference between a stored and requested contract.

    Args:
        path: Stable path identifying the differing contract value.
        stored: Value persisted with the original run.
        requested: Value supplied for the resume attempt.

    Attributes:
        path: Stable path identifying the differing contract value.
        stored: Value persisted with the original run.
        requested: Value supplied for the resume attempt.
    """

    path: str
    stored: Any
    requested: Any


@dataclass(frozen=True, slots=True)
class RunContractComparison:
    """Result of checking whether a durable run can be resumed safely.

    Args:
        compatible: Whether the stored and requested contracts match.
        stored_fingerprint: Fingerprint persisted with the original run.
        requested_fingerprint: Fingerprint supplied for the resume attempt.
        differences: Field-level differences explaining incompatibility.

    Attributes:
        compatible: Whether the stored and requested contracts match.
        stored_fingerprint: Fingerprint persisted with the original run.
        requested_fingerprint: Fingerprint supplied for the resume attempt.
        differences: Field-level differences explaining incompatibility.
    """

    compatible: bool
    stored_fingerprint: str
    requested_fingerprint: str
    differences: tuple[RunContractDifference, ...] = ()

    def describe(self) -> str:
        """Format this comparison as a concise diagnostic message.

        Returns:
            A success message or a mismatch summary containing up to eight
            field differences and the count of any remaining differences.
        """

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
    """Raised when resume inputs differ from the inputs of the original run.

    Args:
        comparison: Comparison containing the incompatible contract details.
    """

    def __init__(self, comparison: RunContractComparison) -> None:
        """Initialize the error with the incompatible contract comparison.

        Args:
            comparison: Comparison containing the fingerprints and field-level differences.
        """

        self.comparison = comparison
        super().__init__(comparison.describe())


class RunStore(Protocol):
    """Required async persistence contract for durable run state.

    Stores persist detached run records, checkpoints, and an ordered per-run
    event stream. Implementations fence writes when the calling store instance
    holds a lease; otherwise they retain the legacy unleased behavior.
    """

    async def create_run(
        self,
        session_id: str,
        *,
        id: str | None = None,
        meta: dict[str, object] | None = None,
        initial_checkpoint: RunCheckpoint | None = None,
        lease: RunLease | None = None,
    ) -> RunRecord:
        """Create a running record or adopt a matching pending record.

        Args:
            session_id: Session that owns the run.
            id: Optional stable run identifier; a UUID is generated when omitted.
            meta: JSON-safe metadata to persist with the run.
            initial_checkpoint: Checkpoint to persist with the new running record.
            lease: Explicit owner/epoch token for pending adoption; omit for an unleased creation.

        Returns:
            The detached running record.

        Raises:
            ValueError: If an existing record is not adoptable, belongs to another
                session, or has an incompatible run contract.
            RunContractMismatchError: If pending-run contract inputs differ.
        """

        ...

    async def load_run(self, run_id: str) -> RunRecord | None:
        """Load a detached run record by identifier.

        Args:
            run_id: Run identifier to look up.

        Returns:
            The run record, or ``None`` when no such run exists.
        """

        ...

    async def save_checkpoint(
        self,
        run_id: str,
        checkpoint: RunCheckpoint,
        *,
        status: str = "running",
        lease: RunLease | None = None,
    ) -> RunRecord:
        """Persist a checkpoint and update the run status.

        Args:
            run_id: Run whose checkpoint should be replaced.
            checkpoint: Resumable state to persist.
            status: Status to store alongside the checkpoint.
            lease: Explicit owner/epoch token; omit for an unleased mutation.

        Returns:
            The detached updated run record.

        Raises:
            KeyError: If the run does not exist.
            LeaseLostError: If the supplied lease token is stale or mismatched.
            TypeError: If checkpoint data cannot be encoded.
        """

        ...

    async def save_checkpoint_with_events(
        self,
        run_id: str,
        checkpoint: RunCheckpoint,
        events: Sequence[Event],
        *,
        status: str = "running",
        lease: RunLease | None = None,
    ) -> tuple[RunRecord, list[int]]:
        """Persist a checkpoint and its pending events as one phase boundary.

        Args:
            run_id: Run whose state should be updated.
            checkpoint: Resumable state to persist.
            events: Events to append in the supplied order.
            status: Status to store alongside the checkpoint.
            lease: Explicit owner/epoch token; omit for an unleased mutation.

        Returns:
            The detached updated record and 1-based sequence numbers parallel to
            ``events``.

        Raises:
            KeyError: If the run does not exist.
            LeaseLostError: If the supplied lease token is stale or mismatched.
            TypeError: If checkpoint or event data cannot be encoded.
        """

        ...

    async def append_event(
        self, run_id: str, event: Event, *, lease: RunLease | None = None
    ) -> int:
        """Append one event and return its 1-based run-local sequence number.

        Args:
            run_id: Run whose event log should receive the event.
            event: Typed event to persist.
            lease: Explicit owner/epoch token; omit for an unleased mutation.

        Returns:
            The sequence number assigned to ``event``.

        Raises:
            KeyError: If the run does not exist.
            LeaseLostError: If the supplied lease token is stale or mismatched.
            TypeError: If an event cannot be encoded.
        """

        ...

    async def load_events(self, run_id: str, *, after_seq: int = 0) -> list[StoredRunEvent]:
        """Load persisted events after an exclusive sequence watermark.

        Args:
            run_id: Run whose event log should be read.
            after_seq: Exclusive sequence watermark; zero reads from the beginning.

        Returns:
            Events in ascending sequence order.
        """

        ...

    async def mark_completed(
        self, run_id: str, checkpoint: RunCheckpoint, *, lease: RunLease | None = None
    ) -> RunRecord:
        """Persist a terminal completed status and checkpoint.

        Args:
            run_id: Run to complete.
            checkpoint: Final checkpoint whose phase is set to ``completed``.
            lease: Explicit owner/epoch token; omit for an unleased mutation.

        Returns:
            The detached completed record.

        Raises:
            KeyError: If the run does not exist.
            LeaseLostError: If the supplied lease token is stale or mismatched.
        """

        ...

    async def mark_failed(
        self,
        run_id: str,
        checkpoint: RunCheckpoint | None = None,
        error: dict[str, object] | None = None,
        *,
        events: Sequence[Event] = (),
        lease: RunLease | None = None,
    ) -> tuple[RunRecord, list[int]]:
        """Persist a failed status, optional checkpoint, error, and events.

        Args:
            run_id: Run to mark failed.
            checkpoint: Optional final checkpoint whose phase becomes ``failed``.
            error: Optional JSON-safe error metadata appended to the run history.
            events: Terminal events to append with the failed state.
            lease: Explicit owner/epoch token; omit for an unleased mutation.

        Returns:
            The detached failed record and 1-based sequence numbers for ``events``.

        Raises:
            KeyError: If the run does not exist.
            LeaseLostError: If the supplied lease token is stale or mismatched.
        """

        ...


class RunEventBatchStore(Protocol):
    """Optional run-store capability for appending several events at once.

    Detected at runtime with ``getattr`` (a store implementing only ``RunStore``
    keeps working via per-event ``append_event``). Implementations assign and
    persist events in input order as one all-or-nothing batch.
    """

    async def append_events(
        self, run_id: str, events: list[Event], *, lease: RunLease | None = None
    ) -> list[int]:
        """Append a batch and return sequence numbers parallel to ``events``.

        Args:
            run_id: Run whose event log should receive the batch.
            events: Typed events to persist in order.
            lease: Explicit owner/epoch token; omit for an unleased mutation.

        Returns:
            The assigned 1-based sequence numbers in input order; an empty
            batch returns ``[]``; a supplied lease is still validated before returning.

        Raises:
            KeyError: If a non-empty batch targets a missing run.
            LeaseLostError: If a non-empty batch uses a stale fencing lease.
            TypeError: If an event in a non-empty batch cannot be encoded.
        """

        ...


class ModelInputSnapshotStore(Protocol):
    """Optional capability for persisting exact pending provider requests."""

    async def save(
        self,
        run_id: str,
        provider_attempt: int,
        request: ProviderRequest,
        *,
        lease: RunLease | None = None,
    ) -> ModelInputSnapshot:
        """Persist a validated provider request snapshot for a run.

        Args:
            run_id: Run that owns the pending provider request.
            provider_attempt: Positive attempt number associated with the request.
            request: Provider request to encode without its live abort signal.
            lease: Explicit owner/epoch token; omit for an unleased mutation.

        Returns:
            The immutable encoded snapshot.

        Raises:
            KeyError: If the run does not exist.
            LeaseLostError: If the supplied lease token is stale or mismatched.
            ValueError: If ``provider_attempt`` is invalid.
            TypeError: If the request contains unsupported data.
            ModelInputSnapshotError: If the request cannot be encoded safely.
        """

        ...

    async def load(self, snapshot_id: str) -> ModelInputSnapshot | None:
        """Load and integrity-check a stored provider request snapshot.

        Args:
            snapshot_id: Snapshot identifier to look up.

        Returns:
            The validated snapshot, or ``None`` when it does not exist.

        Raises:
            ModelInputSnapshotError: If the snapshot is corrupt or unsupported.
        """

        ...

    async def delete(self, snapshot_id: str, *, run_id: str, lease: RunLease | None = None) -> None:
        """Delete one snapshot only when it belongs to the specified run.

        Args:
            snapshot_id: Snapshot identifier to delete.
            run_id: Run that must own the snapshot.
            lease: Explicit owner/epoch token; omit for an unleased mutation.

        Raises:
            LeaseLostError: If the supplied lease token is stale or mismatched.

        A snapshot belonging to another run, or an unknown snapshot, is not
        deleted. Backends may report an unknown run according to their storage
        semantics.
        """

        ...

    async def prune(
        self, run_id: str, keep_ids: Sequence[str] = (), *, lease: RunLease | None = None
    ) -> int:
        """Delete snapshots for a run except identifiers explicitly retained.

        Args:
            run_id: Run whose snapshots should be pruned.
            keep_ids: Snapshot identifiers to retain.
            lease: Explicit owner/epoch token; omit for an unleased mutation.

        Returns:
            Number of snapshots deleted.

        Raises:
            LeaseLostError: If the supplied lease token is stale or mismatched.

        Backends may report an unknown run according to their storage semantics.
        """

        ...


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

    return walk_json(
        value,
        check_node=_check_model_json_node,
        check_key=_check_model_json_key,
        path=path,
        seen=set(seen),
        depth=depth,
    )


def _check_model_json_node(value: Any, path: str, seen: set[int], depth: int) -> int | None:
    if value is None or isinstance(value, str | bool | int):
        return None
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError(f"{path} contains a non-finite float")
        return None
    if depth >= _JSON_SAFE_MAX_DEPTH:
        raise TypeError(f"{path} exceeds maximum nesting depth {_JSON_SAFE_MAX_DEPTH}")
    if not isinstance(value, Mapping | list):
        raise TypeError(f"{path} contains non-JSON value of type {type(value).__name__}")
    if id(value) in seen:
        raise TypeError(f"{path} contains a recursive value")
    return depth + 1


def _check_model_json_key(key: Any, path: str) -> None:
    if not isinstance(key, str):
        raise TypeError(f"{path} mapping key must be str, got {type(key).__name__}")


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
    """Strictly encode a provider request while excluding its live signal.

    The resulting canonical JSON and integrity hash can be persisted before a
    provider call and decoded later with a newly supplied abort signal.

    Args:
        run_id: Run associated with the pending provider request.
        provider_attempt: Positive, 1-based provider dispatch attempt.
        request: Provider request to encode.
        id: Optional snapshot identifier; a UUID is generated when omitted.
        created_at: Optional creation timestamp; the current timestamp is used when omitted.

    Returns:
        An immutable, integrity-checkable model-input snapshot.

    Raises:
        ValueError: If the run ID, attempt number, or snapshot ID is invalid.
        TypeError: If the request contains unsupported or non-JSON data.
        ModelInputSnapshotError: If the codec cannot cover the request shape.
    """

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
    require_wire_version(snapshot.codec_version)
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
    """Verify and decode a snapshot, rebinding only the caller's live signal.

    Args:
        snapshot: Encoded snapshot to validate and decode.
        signal: Live abort signal to attach to the reconstructed request.

    Returns:
        A provider request reconstructed from the integrity-checked payload.

    Raises:
        ModelInputSnapshotError: If the snapshot is corrupt, unsupported, or has
            an invalid provider-request shape.
    """

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

    Args:
        primary_model: Resolved primary provider model identifier.
        fallback_models: Ordered fallback model identifiers.
        system_blocks: Resolved system-prompt blocks included in the request.
        tool_schemas: Provider-facing tool schemas in their offered order.
        input_payload: Canonical input values that affect execution.
        output_schema: Structured-output schema, if configured.
        final_tool_name: Name of the terminal tool, if configured.
        run_options: Resolved run options that affect execution.
        budget: Budget limits or a mapping of budget policy values.
        policies: Resolved policy and reliability settings.

    Returns:
        A canonical contract with a deterministic fingerprint.

    Raises:
        ValueError: If a model identifier is empty.
        TypeError: If an input contains a value that cannot be represented safely.
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
    """Encode a run contract with an integrity-checkable fingerprint.

    Args:
        contract: Contract whose current wire envelope should be serialized.

    Returns:
        JSON-safe envelope containing the schema version, fingerprint, and payload.

    Raises:
        ValueError: If the fingerprint does not match the payload.
        TypeError: If the payload contains a value that cannot be serialized safely.
    """

    require_wire_version(contract.schema_version)
    payload = _json_safe(contract.payload, strict=True)
    if contract.fingerprint != _run_contract_fingerprint(contract.schema_version, payload):
        raise ValueError("run contract fingerprint does not match payload")
    return {
        "schema_version": contract.schema_version,
        "fingerprint": contract.fingerprint,
        "payload": payload,
    }


def run_contract_from_dict(raw: Mapping[str, Any]) -> RunContract:
    """Decode a current contract envelope while retaining its open payload.

    Args:
        raw: Mapping containing the current schema version, fingerprint, and payload.

    Returns:
        A validated run contract.

    Raises:
        ValueError: If the envelope, payload, or fingerprint is malformed.
        UnsupportedWireVersionError: If the envelope is missing or uses an unsupported
            wire version.
    """

    if not isinstance(raw, Mapping):
        require_wire_version(None)
    require_wire_version(raw.get("schema_version"))
    version_raw = raw["schema_version"]
    payload_raw = raw.get("payload")
    if not isinstance(payload_raw, Mapping):
        raise ValueError("run contract payload must be a mapping")
    payload = _json_safe(payload_raw, strict=True)
    assert isinstance(payload, dict)
    computed = _run_contract_fingerprint(version_raw, payload)
    declared = raw.get("fingerprint")
    if not isinstance(declared, str) or not declared or declared != computed:
        raise ValueError("run contract fingerprint does not match payload")
    return RunContract(schema_version=version_raw, payload=payload, fingerprint=declared)


def run_meta_with_contract(
    contract: RunContract,
    meta: Mapping[str, Any] | None = None,
) -> dict[str, object]:
    """Return JSON-safe run metadata carrying ``contract`` without mutation.

    Args:
        contract: Contract to place under the run-contract metadata key.
        meta: Optional existing metadata to copy and extend.

    Returns:
        A detached metadata mapping containing the serialized contract.

    Raises:
        ValueError: If the contract fingerprint is invalid.
        TypeError: If contract data cannot be represented safely.
    """

    safe = _json_safe(meta or {}, strict=False)
    assert isinstance(safe, dict)
    safe[RUN_CONTRACT_META_KEY] = run_contract_to_dict(contract)
    return safe


def run_contract_from_meta(meta: Mapping[str, Any]) -> RunContract:
    """Load a current persisted contract, rejecting contractless runs.

    Args:
        meta: Persisted run metadata containing the run-contract entry.

    Returns:
        The validated contract embedded in ``meta``.

    Raises:
        ValueError: If the embedded contract envelope is malformed.
        UnsupportedWireVersionError: If the metadata has no current contract or
            uses an unsupported wire version.
    """

    if RUN_CONTRACT_META_KEY not in meta:
        require_wire_version(None)
    raw = meta[RUN_CONTRACT_META_KEY]
    if not isinstance(raw, Mapping):
        require_wire_version(None)
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
) -> RunContractComparison:
    """Compare persisted and requested execution inputs field by field.

    Args:
        stored: Contract persisted with the original run.
        requested: Contract resolved for the resume attempt.

    Returns:
        A comparison containing compatibility, fingerprints, and differences.

    Raises:
        UnsupportedWireVersionError: If ``stored`` is missing, as contractless
            runs are not resumable.
    """

    if stored is None:
        require_wire_version(None)
    assert stored is not None
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
        stored_fingerprint=stored.fingerprint,
        requested_fingerprint=requested.fingerprint,
        differences=tuple(differences),
    )


def ensure_run_contract_compatible(
    stored: RunContract | None,
    requested: RunContract,
) -> RunContractComparison:
    """Validate resume inputs and return their compatibility comparison.

    Args:
        stored: Contract persisted with the original run.
        requested: Contract resolved for the resume attempt.

    Returns:
        A compatible contract comparison.

    Raises:
        RunContractMismatchError: If any stored and requested execution inputs differ.
        UnsupportedWireVersionError: If ``stored`` is missing, as contractless
            runs are not resumable.
    """

    comparison = compare_run_contract(stored, requested)
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
    data["extension_state"] = checkpoint.extension_state
    data["provider_attempt"] = checkpoint.provider_attempt
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
    """Preserve extension payloads; each extension validates its own namespace."""
    if not isinstance(value, dict):
        return {}
    return {key: item for key, item in value.items() if isinstance(key, str)}


def _require_tool_recovery_cursor(value: int | None) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("tool recovery requires a non-negative integer cursor")
    return value


def checkpoint_from_dict(raw: dict[str, Any]) -> RunCheckpoint:
    if not isinstance(raw, dict):
        require_wire_version(None)
    require_wire_version(raw.get("schema_version"))
    cursor = raw["tool_batch_event_after_seq"]
    if cursor is not None:
        _require_tool_recovery_cursor(cursor)
    assistant_raw = raw["assistant_message"]
    pending_blocks: list[ToolUseBlock] = []
    for item in raw["pending_tool_blocks"]:
        if isinstance(item, dict):
            block = block_from_dict(item)
            if isinstance(block, ToolUseBlock):
                pending_blocks.append(block)

    completed: dict[str, ToolResultBlock] = {}
    completed_raw = raw["completed_tool_results"]
    if isinstance(completed_raw, dict):
        for key, item in completed_raw.items():
            if isinstance(item, dict):
                block = block_from_dict(item)
                if isinstance(block, ToolResultBlock):
                    completed[str(key)] = block

    return RunCheckpoint(
        phase=raw["phase"],
        prompt=str(raw["prompt"]),
        turn_index=int(raw["turn_index"] or 0),
        total_usage=usage_from_dict(dict(raw["total_usage"])),
        assistant_message=(
            message_from_dict(dict(assistant_raw)) if isinstance(assistant_raw, dict) else None
        ),
        pending_tool_blocks=pending_blocks,
        completed_tool_results=completed,
        force_final_pending=bool(raw["force_final_pending"]),
        loop_guard_state=_dict_or_none(raw["loop_guard_state"]),
        pending_skill_overlay=_dict_or_none(raw["pending_skill_overlay"]),
        current_turn_allowed_tools=(
            [str(t) for t in raw["current_turn_allowed_tools"]]
            if isinstance(raw["current_turn_allowed_tools"], list)
            else None
        ),
        assistant_stop_reason=(
            str(raw["assistant_stop_reason"])
            if isinstance(raw["assistant_stop_reason"], str)
            else None
        ),
        permission_decisions=(
            {str(k): dict(v) for k, v in raw["permission_decisions"].items()}
            if isinstance(raw["permission_decisions"], dict)
            else {}
        ),
        background_workers=(
            {str(k): dict(v) for k, v in raw["background_workers"].items()}
            if isinstance(raw["background_workers"], dict)
            else {}
        ),
        truncation_attempts=max(0, int(raw["truncation_attempts"] or 0)),
        truncation_prefix=(
            str(raw["truncation_prefix"]) if isinstance(raw["truncation_prefix"], str) else ""
        ),
        pending_truncation_feedback=(
            str(raw["pending_truncation_feedback"])
            if isinstance(raw["pending_truncation_feedback"], str)
            else None
        ),
        tool_batch_event_after_seq=cursor,
        pending_alignment=_pending_alignment_from_raw(raw["pending_alignment"]),
        extension_state=_extension_state_from_raw(raw["extension_state"]),
        provider_attempt=(
            max(0, raw["provider_attempt"])
            if isinstance(raw["provider_attempt"], int)
            and not isinstance(raw["provider_attempt"], bool)
            else 0
        ),
        model_input_snapshot_id=(
            raw["model_input_snapshot_id"]
            if isinstance(raw["model_input_snapshot_id"], str) and raw["model_input_snapshot_id"]
            else None
        ),
    )


def _safe_meta(meta: Any) -> dict[str, object]:
    safe = _json_safe(meta, strict=False)
    return safe if isinstance(safe, dict) else {}


def _append_run_error(meta: dict[str, object], error: dict[str, object]) -> None:
    """Append to current error-object history, replacing malformed history."""

    existing = meta.get("errors")
    if not isinstance(existing, list) or not all(isinstance(item, dict) for item in existing):
        existing = []
        meta["errors"] = existing
    existing.append(_json_safe(error, strict=False))


def _copy_checkpoint(checkpoint: RunCheckpoint | None) -> RunCheckpoint | None:
    if checkpoint is None:
        return None
    return checkpoint_from_dict(checkpoint_to_dict(checkpoint))


def _copy_record(record: RunRecord) -> RunRecord:
    # In-memory writes already normalize these fields; only detach the returned snapshot.
    return RunRecord(
        id=record.id,
        session_id=record.session_id,
        status=record.status,
        created_at=record.created_at,
        updated_at=record.updated_at,
        checkpoint=deepcopy(record.checkpoint),
        meta=deepcopy(record.meta),
    )


def _json_safe_stored_event(event: Event) -> dict[str, Any]:
    """Normalize the payload before adding the outer envelope.

    The event payload's depth budget is shared with the P06 ignorable-event
    validator. Applying ``_json_safe`` to the wrapper would spend one level on
    the durability envelope and truncate a payload that was valid before the
    envelope was introduced.
    """

    envelope = _stored_event_to_dict(event)
    payload = _json_safe(envelope["event"], strict=False)
    assert isinstance(payload, dict)
    return {"schema_version": envelope["schema_version"], "event": payload}


def _copy_event(event: Event) -> Event:
    return _stored_event_from_dict(_json_safe_stored_event(event))


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


def _validate_lease_ttl(ttl_s: float) -> float:
    if isinstance(ttl_s, bool) or not isinstance(ttl_s, (int, float)):
        raise ValueError("lease ttl_s must be a finite positive number")
    ttl = float(ttl_s)
    if not math.isfinite(ttl) or ttl <= 0:
        raise ValueError("lease ttl_s must be a finite positive number")
    return ttl


def _lease_lost(run_id: str, expected: int, actual: int | None = None) -> LeaseLostError:
    detail = f" (stored epoch {actual})" if actual is not None else ""
    return LeaseLostError(f"lease lost for run {run_id}: expected epoch {expected}{detail}")


def _supports_lease_keyword(method: Any) -> bool:
    """Report whether a callable can receive the explicit lease keyword."""
    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        return False
    lease_parameter = signature.parameters.get("lease")
    return (
        lease_parameter is not None
        and lease_parameter.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    ) or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )


def _lease_kwargs(method: Any, lease: RunLease | None) -> dict[str, Any]:
    """Build a mutation keyword mapping without dropping an explicit lease.

    Unleased calls omit the keyword so custom stores retain their existing
    signatures. A leased call fails before invoking an override that cannot
    receive the token; callers must never retry that call without its fence.
    """
    if lease is None:
        return {}
    if not _supports_lease_keyword(method):
        raise TypeError(f"{getattr(method, '__qualname__', method)!s} does not support lease")
    return {"lease": lease}


def _validate_lease_run(run_id: str, lease: RunLease | None) -> None:
    """Reject malformed or wrong-run tokens before a mutation starts."""
    if lease is None:
        return
    if lease.run_id != run_id:
        raise LeaseLostError(f"lease run {lease.run_id} does not match target run {run_id}")
    if not isinstance(lease.owner, str):
        raise LeaseLostError(f"lease for run {run_id} has no valid owner")
    if isinstance(lease.epoch, bool) or not isinstance(lease.epoch, int) or lease.epoch < 1:
        raise LeaseLostError(f"lease for run {run_id} has no valid epoch")


def _preflight_leased_store(
    store: Any,
    run_id: str,
    lease: RunLease | None,
    *,
    include_snapshots: bool = False,
) -> None:
    """Check leased mutation signatures before an execution has side effects."""
    if lease is None:
        return
    _validate_lease_run(run_id, lease)
    _preflight_lease_capabilities(store, include_snapshots=include_snapshots)


def _preflight_lease_capabilities(
    store: Any,
    *,
    include_snapshots: bool = False,
    include_lifecycle: bool = False,
) -> None:
    """Check leased mutation and optional lifecycle signatures before setup."""
    required = (
        "create_run",
        "save_checkpoint",
        "save_checkpoint_with_events",
        "append_event",
        "mark_completed",
        "mark_failed",
    )
    optional = ("append_events",)
    snapshot_methods: tuple[str, ...] = ()
    if include_snapshots:
        snapshot_methods = ("save", "delete", "prune")
        optional += snapshot_methods
    lifecycle = ("claim", "renew", "release") if include_lifecycle else ()
    for name in (*required, *optional, *lifecycle):
        method = getattr(store, name, None)
        if not callable(method):
            if name in required or name in snapshot_methods or name in lifecycle:
                raise ConfigError(f"run store lease capability missing {name}")
            continue
        if not _supports_lease_keyword(method) and name not in {"claim", "renew", "release"}:
            raise TypeError(f"{getattr(method, '__qualname__', method)!s} does not support lease")


class InMemoryRunStore:
    """Keep runs, events, leases, and model-input snapshots in process memory.

    This backend implements the run-store and optional lease/snapshot
    capabilities for tests and local development. Returned records and events
    are detached copies; state is lost when the process exits.
    """

    def __init__(self) -> None:
        """Initialize an empty in-memory run store."""

        self._runs: dict[str, RunRecord] = {}
        self._events: dict[str, list[StoredRunEvent]] = {}
        self._model_input_snapshots: dict[str, ModelInputSnapshot] = {}
        # The durable lease state is shared by copied test-store handles.  A
        # caller's immutable RunLease is the only holder authority.
        self._lease_state: dict[str, tuple[str | None, float | None, int]] = {}

    async def create_run(
        self,
        session_id: str,
        *,
        id: str | None = None,
        meta: dict[str, object] | None = None,
        initial_checkpoint: RunCheckpoint | None = None,
        lease: RunLease | None = None,
    ) -> RunRecord:
        """Create a running record or adopt a matching pending record.

        Args:
            session_id: Session that owns the run.
            id: Optional stable run identifier; a UUID is generated when omitted.
            meta: JSON-safe metadata to persist with the run.
            initial_checkpoint: Checkpoint to persist with the running record.
            lease: Explicit owner/epoch token for pending adoption; omit for an unleased creation.

        Returns:
            A detached running record.

        Raises:
            ValueError: If an existing record is not pending, belongs to another
                session, or has an incompatible run contract.
            RunContractMismatchError: If pending-run contract inputs differ.
            LeaseLostError: If a held lease was replaced before pending adoption.
        """

        checkpoint = _copy_checkpoint(initial_checkpoint)
        rid = id or str(uuid4())
        existing = self._runs.get(rid)
        if existing is not None:
            _validate_lease_run(rid, lease)
            self._check_lease(rid, lease)
            if existing.status != "pending":
                raise ValueError(f"run {rid} already exists")
            if existing.session_id != session_id:
                raise ValueError(f"run {rid} belongs to session {existing.session_id}")
            incoming_meta = _safe_meta(meta)
            stored_contract = existing.meta.get(RUN_CONTRACT_META_KEY)
            requested_contract = incoming_meta.get(RUN_CONTRACT_META_KEY)
            if stored_contract is not None and requested_contract is not None:
                ensure_run_contract_compatible(
                    run_contract_from_meta(existing.meta),
                    run_contract_from_meta(incoming_meta),
                )
            merged_meta = _safe_meta(existing.meta)
            merged_meta.update(incoming_meta)
            if stored_contract is not None:
                merged_meta[RUN_CONTRACT_META_KEY] = stored_contract
            existing.meta = merged_meta
            existing.status = "running"
            existing.checkpoint = checkpoint
            existing.updated_at = now_iso()
            return _copy_record(existing)
        if lease is not None:
            raise _lease_lost(rid, lease.epoch)
        ts = now_iso()
        record = RunRecord(
            id=rid,
            session_id=session_id,
            status="running",
            created_at=ts,
            updated_at=ts,
            meta=_safe_meta(meta),
            checkpoint=checkpoint,
        )
        self._runs[rid] = record
        self._events[rid] = []
        self._lease_state.setdefault(rid, (None, None, 0))
        return _copy_record(record)

    async def claim(self, run_id: str, owner: str, ttl_s: float) -> RunLease:
        """Claim a run and return its current fencing lease.

        Args:
            run_id: Existing run to claim.
            owner: Unique worker or process identifier.
            ttl_s: Positive finite lease duration in seconds.

        Returns:
            An immutable lease with the run's fencing epoch and expiry.

        Raises:
            KeyError: If ``run_id`` does not identify a run.
            RunLeaseConflictError: If another owner holds a live lease.
            ValueError: If ``ttl_s`` is not positive and finite.
        """

        ttl = _validate_lease_ttl(ttl_s)
        if run_id not in self._runs:
            raise KeyError(f"run not found: {run_id}")
        current_owner, expires_at, epoch = self._lease_state.get(run_id, (None, None, 0))
        now = time.time()
        live = current_owner is not None and expires_at is not None and expires_at > now
        if live and current_owner != owner:
            raise RunLeaseConflictError(f"run {run_id} is leased by {current_owner}")
        next_epoch = epoch if live and current_owner == owner else epoch + 1
        expires = now + ttl
        self._lease_state[run_id] = (owner, expires, next_epoch)
        return RunLease(run_id, owner, next_epoch, expires, ttl)

    async def renew(self, lease: RunLease) -> RunLease:
        """Extend a held lease without changing its fencing epoch.

        Args:
            lease: Immutable owner, epoch, and renewal-duration token.

        Returns:
            The same lease identity with a refreshed expiry timestamp.

        Raises:
            LeaseLostError: If the lease is stale, expired, or no longer held.
        """

        _validate_lease_run(lease.run_id, lease)
        current_owner, expires_at, epoch = self._lease_state.get(lease.run_id, (None, None, 0))
        if (
            epoch != lease.epoch
            or current_owner != lease.owner
            or expires_at is None
            or expires_at <= time.time()
        ):
            raise _lease_lost(lease.run_id, lease.epoch, epoch)
        ttl = _validate_lease_ttl(lease.ttl_s)
        renewed = time.time() + ttl
        self._lease_state[lease.run_id] = (lease.owner, renewed, lease.epoch)
        return RunLease(lease.run_id, lease.owner, lease.epoch, renewed, ttl)

    async def release(self, lease: RunLease) -> None:
        """Release a matching lease and advance its fencing epoch.

        Args:
            lease: Lease whose ownership should be relinquished.

        Raises:
            LeaseLostError: If the lease no longer matches the stored owner or epoch.
        """

        _validate_lease_run(lease.run_id, lease)
        current_owner, _expires_at, epoch = self._lease_state.get(lease.run_id, (None, None, 0))
        if epoch != lease.epoch or current_owner != lease.owner:
            raise _lease_lost(lease.run_id, lease.epoch, epoch)
        self._lease_state[lease.run_id] = (None, None, lease.epoch + 1)

    def _check_lease(self, run_id: str, lease: RunLease | None = None) -> None:
        _validate_lease_run(run_id, lease)
        if lease is None:
            return
        current = self._lease_state.get(run_id)
        if current is None:
            raise _lease_lost(run_id, lease.epoch)
        current_owner, _expires_at, actual = current
        if actual != lease.epoch or current_owner != lease.owner:
            raise _lease_lost(run_id, lease.epoch, actual)

    async def load_run(self, run_id: str) -> RunRecord | None:
        """Load a detached run record by identifier.

        Args:
            run_id: Run identifier to look up.

        Returns:
            The detached record, or ``None`` when no such run exists.
        """

        record = self._runs.get(run_id)
        return _copy_record(record) if record is not None else None

    async def save_checkpoint(
        self,
        run_id: str,
        checkpoint: RunCheckpoint,
        *,
        status: str = "running",
        lease: RunLease | None = None,
    ) -> RunRecord:
        """Persist a checkpoint and update the run status.

        Args:
            run_id: Run whose checkpoint should be replaced.
            checkpoint: Resumable state to persist.
            status: Status to store alongside the checkpoint.
            lease: Explicit owner/epoch token; omit for an unleased mutation.

        Returns:
            A detached updated run record.

        Raises:
            KeyError: If the run does not exist.
            LeaseLostError: If the supplied lease token is stale or mismatched.
        """

        self._check_lease(run_id, lease)
        rec = self._runs[run_id]
        rec.checkpoint = _copy_checkpoint(checkpoint)
        rec.status = status  # type: ignore[assignment]
        rec.updated_at = now_iso()
        return _copy_record(rec)

    async def save_checkpoint_with_events(
        self,
        run_id: str,
        checkpoint: RunCheckpoint,
        events: Sequence[Event],
        *,
        status: str = "running",
        lease: RunLease | None = None,
    ) -> tuple[RunRecord, list[int]]:
        """Atomically publish a checkpoint and its pending event prefix.

        Args:
            run_id: Run whose state should be updated.
            checkpoint: Resumable state to persist.
            events: Events to append in the supplied order.
            status: Status to store alongside the checkpoint.
            lease: Explicit owner/epoch token; omit for an unleased mutation.

        Returns:
            The detached updated record and sequence numbers parallel to ``events``.

        Raises:
            KeyError: If the run does not exist.
            LeaseLostError: If the supplied lease token is stale or mismatched.
            TypeError: If an event or checkpoint cannot be encoded.
        """

        self._check_lease(run_id, lease)
        rec = self._runs[run_id]
        checkpoint_copy = _copy_checkpoint(checkpoint)
        event_copies = [_copy_event(event) for event in events]
        bucket = self._events.setdefault(run_id, [])
        timestamp = now_iso()
        seqs = list(range(len(bucket) + 1, len(bucket) + len(event_copies) + 1))
        rec.checkpoint = checkpoint_copy
        rec.status = status  # type: ignore[assignment]
        rec.updated_at = timestamp
        bucket.extend(
            StoredRunEvent(seq=seq, appended_at=timestamp, event=event)
            for seq, event in zip(seqs, event_copies, strict=True)
        )
        return _copy_record(rec), seqs

    async def append_event(
        self, run_id: str, event: Event, *, lease: RunLease | None = None
    ) -> int:
        """Append one event and return its 1-based sequence number.

        Args:
            run_id: Run whose event log should receive the event.
            event: Typed event to persist.
            lease: Explicit owner/epoch token; omit for an unleased mutation.

        Returns:
            The sequence number assigned to ``event``.

        Raises:
            KeyError: If the run does not exist.
            LeaseLostError: If the supplied lease token is stale or mismatched.
            TypeError: If the event cannot be encoded.
        """

        self._check_lease(run_id, lease)
        if run_id not in self._runs:
            raise KeyError(f"run not found: {run_id}")
        bucket = self._events.setdefault(run_id, [])
        seq = len(bucket) + 1
        bucket.append(StoredRunEvent(seq=seq, appended_at=now_iso(), event=_copy_event(event)))
        return seq

    async def append_events(
        self, run_id: str, events: list[Event], *, lease: RunLease | None = None
    ) -> list[int]:
        """Atomically append events in input order.

        Args:
            run_id: Run whose event log should receive the events.
            events: Typed events to append in input order.
            lease: Explicit owner/epoch token; omit for an unleased mutation.

        Returns:
            Sequence numbers returned for each input event, in the same order;
            an empty batch returns ``[]``; a supplied lease is still validated.

        Raises:
            KeyError: If a non-empty batch targets a missing run.
            LeaseLostError: If a non-empty batch uses a stale fencing lease.
            TypeError: If an event in a non-empty batch cannot be encoded.
        """

        if not events:
            if lease is not None:
                self._check_lease(run_id, lease)
            return []
        self._check_lease(run_id, lease)
        if run_id not in self._runs:
            raise KeyError(f"run not found: {run_id}")
        copies = [_copy_event(event) for event in events]
        if type(self).append_event is not InMemoryRunStore.append_event:
            return [
                await self.append_event(run_id, event, **_lease_kwargs(self.append_event, lease))
                for event in copies
            ]
        bucket = self._events.setdefault(run_id, [])
        timestamp = now_iso()
        seqs = list(range(len(bucket) + 1, len(bucket) + len(copies) + 1))
        bucket.extend(
            StoredRunEvent(seq=seq, appended_at=timestamp, event=event)
            for seq, event in zip(seqs, copies, strict=True)
        )
        return seqs

    async def load_events(self, run_id: str, *, after_seq: int = 0) -> list[StoredRunEvent]:
        """Load detached events after an exclusive sequence watermark.

        Args:
            run_id: Run whose event log should be read.
            after_seq: Exclusive sequence watermark; zero reads from the beginning.

        Returns:
            Events in ascending sequence order.
        """

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
        *,
        lease: RunLease | None = None,
    ) -> ModelInputSnapshot:
        """Persist an exact provider-request snapshot for a run.

        Args:
            run_id: Run that owns the pending provider request.
            provider_attempt: Positive, 1-based provider dispatch attempt.
            request: Provider request to encode without its live abort signal.
            lease: Explicit owner/epoch token; omit for an unleased mutation.

        Returns:
            The immutable encoded snapshot.

        Raises:
            KeyError: If the run does not exist.
            LeaseLostError: If the supplied lease token is stale or mismatched.
            ValueError: If ``provider_attempt`` is invalid.
            TypeError: If the request contains unsupported data.
            ModelInputSnapshotError: If snapshot validation fails.
        """

        self._check_lease(run_id, lease)
        if run_id not in self._runs:
            raise KeyError(f"run not found: {run_id}")
        snapshot = create_model_input_snapshot(run_id, provider_attempt, request)
        self._model_input_snapshots[snapshot.id] = snapshot
        return snapshot

    async def load(self, snapshot_id: str) -> ModelInputSnapshot | None:
        """Load and validate a stored provider-request snapshot.

        Args:
            snapshot_id: Snapshot identifier to look up.

        Returns:
            The validated snapshot, or ``None`` when it does not exist.

        Raises:
            ModelInputSnapshotError: If the stored snapshot is corrupt or unsupported.
        """

        snapshot = self._model_input_snapshots.get(snapshot_id)
        return _validate_model_input_snapshot(snapshot) if snapshot is not None else None

    async def delete(self, snapshot_id: str, *, run_id: str, lease: RunLease | None = None) -> None:
        """Delete a snapshot only when it belongs to the specified run.

        Args:
            snapshot_id: Snapshot identifier to delete.
            run_id: Run that must own the snapshot.
            lease: Explicit owner/epoch token; omit for an unleased mutation.

        Raises:
            LeaseLostError: If the supplied lease token is stale or mismatched.
        """

        self._check_lease(run_id, lease)
        snapshot = self._model_input_snapshots.get(snapshot_id)
        if snapshot is not None and snapshot.run_id != run_id:
            return
        self._model_input_snapshots.pop(snapshot_id, None)

    async def prune(
        self, run_id: str, keep_ids: Sequence[str] = (), *, lease: RunLease | None = None
    ) -> int:
        """Delete all run snapshots except explicitly retained identifiers.

        Args:
            run_id: Run whose snapshots should be pruned.
            keep_ids: Snapshot identifiers to retain.
            lease: Explicit owner/epoch token; omit for an unleased mutation.

        Returns:
            Number of snapshots deleted.

        Raises:
            LeaseLostError: If the supplied lease token is stale or mismatched.
        """

        self._check_lease(run_id, lease)
        keep = set(keep_ids)
        doomed = [
            snapshot_id
            for snapshot_id, snapshot in self._model_input_snapshots.items()
            if snapshot.run_id == run_id and snapshot_id not in keep
        ]
        for snapshot_id in doomed:
            del self._model_input_snapshots[snapshot_id]
        return len(doomed)

    async def mark_completed(
        self, run_id: str, checkpoint: RunCheckpoint, *, lease: RunLease | None = None
    ) -> RunRecord:
        """Persist a terminal completed status and checkpoint.

        Args:
            run_id: Run to complete.
            checkpoint: Final checkpoint whose phase is set to ``completed``.
            lease: Explicit owner/epoch token; omit for an unleased mutation.

        Returns:
            A detached completed record.

        Raises:
            KeyError: If the run does not exist.
            LeaseLostError: If the supplied lease token is stale or mismatched.
        """

        self._check_lease(run_id, lease)
        checkpoint.phase = "completed"
        return await self.save_checkpoint(
            run_id,
            checkpoint,
            status="completed",
            **_lease_kwargs(self.save_checkpoint, lease),
        )

    async def mark_failed(
        self,
        run_id: str,
        checkpoint: RunCheckpoint | None = None,
        error: dict[str, object] | None = None,
        *,
        events: Sequence[Event] = (),
        lease: RunLease | None = None,
    ) -> tuple[RunRecord, list[int]]:
        """Persist a failed status, optional checkpoint, error, and events.

        Args:
            run_id: Run to mark failed.
            checkpoint: Optional final checkpoint whose phase becomes ``failed``.
            error: Optional JSON-safe error metadata appended to the run history.
            events: Terminal events to append with the failed state.
            lease: Explicit owner/epoch token; omit for an unleased mutation.

        Returns:
            The detached failed record and sequence numbers parallel to ``events``.

        Raises:
            KeyError: If the run does not exist.
            LeaseLostError: If the supplied lease token is stale or mismatched.
            TypeError: If a checkpoint or event cannot be encoded.
        """

        self._check_lease(run_id, lease)
        rec = self._runs[run_id]
        checkpoint_copy = _copy_checkpoint(checkpoint) if checkpoint is not None else rec.checkpoint
        event_copies = [_copy_event(event) for event in events]
        meta = _safe_meta(rec.meta)
        if error is not None:
            _append_run_error(meta, error)
        if checkpoint is not None:
            checkpoint.phase = "failed"
            assert checkpoint_copy is not None
            checkpoint_copy.phase = "failed"
        bucket = self._events[run_id]
        timestamp = now_iso()
        seqs = list(range(len(bucket) + 1, len(bucket) + len(event_copies) + 1))
        rec.checkpoint, rec.meta = checkpoint_copy, meta
        rec.status, rec.updated_at = "failed", timestamp
        bucket.extend(
            StoredRunEvent(seq=seq, appended_at=timestamp, event=event)
            for seq, event in zip(seqs, event_copies, strict=True)
        )
        return _copy_record(rec), seqs

    async def close(self) -> None:
        """Close the in-memory store; no resources require cleanup."""

        return None
