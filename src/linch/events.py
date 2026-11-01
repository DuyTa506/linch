from __future__ import annotations

import json
import math
from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any, Literal, TypeAlias, cast

from ._json import walk_json
from ._wire import WIRE_VERSION, require_wire_version
from .tools.base import (
    CanonicalToolOutput,
    Citation,
    ToolAttachment,
    ToolOutput,
    ToolOutputError,
    ToolResult,
    normalize_tool_output,
)
from .types import Message, StopReason, Usage, message_from_dict, message_to_dict


@dataclass(slots=True)
class SystemEvent:
    """Describe the session and tools at run initialization.

    Args:
        session_id: Session that owns the run.
        run_id: Run identifier.
        model: Model selected for the run.
        tools: Names of tools offered to the model.
        permission_mode: Permission policy active for the run.
        cwd: Working directory used by the run.
        type: Wire discriminator for the event.
        subtype: System-event subtype.
        trace_id: Optional trace identifier associated with the run.
    """

    session_id: str
    run_id: str
    model: str
    tools: list[str]
    permission_mode: str
    cwd: str
    type: Literal["system"] = "system"
    subtype: Literal["init"] = "init"
    trace_id: str | None = None


@dataclass(slots=True)
class UserEvent:
    """Record a user-authored or user-visible message event.

    Args:
        message: Message carried by the event.
        type: Wire discriminator for the event.
        subtype: Kind of user event represented.
    """

    message: Message
    type: Literal["user"] = "user"
    subtype: Literal["prompt", "tool_result", "alignment", "notification"] = "prompt"


@dataclass(slots=True)
class AssistantEvent:
    """Record a completed assistant response.

    Args:
        message: Assistant message returned by the provider.
        stop_reason: Reason the provider stopped generating.
        type: Wire discriminator for the event.
    """

    message: Message
    stop_reason: StopReason
    type: Literal["assistant"] = "assistant"


@dataclass(slots=True)
class PartialAssistantEvent:
    """Record a transient assistant delta while a response streams.

    Attributes:
        delta: Provider-normalized fields emitted for the current stream update.

    Args:
        delta: Provider-normalized delta payload.
        type: Wire discriminator for the event.
    """

    delta: dict[str, Any]
    type: Literal["partial_assistant"] = "partial_assistant"


@dataclass(slots=True)
class ToolCallStartEvent:
    """Record the start of one tool invocation.

    Args:
        tool_use_id: Identifier assigned to the tool call.
        tool_name: Name of the tool being invoked.
        input: JSON-compatible input supplied to the tool.
        summary: Human-readable summary of the invocation.
        type: Wire discriminator for the event.
    """

    tool_use_id: str
    tool_name: str
    input: dict[str, Any]
    summary: str
    type: Literal["tool_call_start"] = "tool_call_start"


@dataclass(slots=True)
class ToolCallEndEvent:
    """Record the completed result of one tool invocation.

    Attributes:
        result: Textual projection of the completed result.
        tool_result: Normalized legacy structured result, when supplied.
        tool_output: Canonical V2 output retained alongside its result projections,
            when supplied.

    Args:
        tool_use_id: Identifier assigned to the tool call.
        tool_name: Name of the invoked tool.
        result: Textual result exposed for compatibility.
        is_error: Whether the invocation failed.
        duration_ms: Elapsed invocation time in milliseconds.
        tool_result: Normalized structured result, when available.
        type: Wire discriminator for the event.
        tool_output: Canonical output, when the tool returned one.
    """

    tool_use_id: str
    tool_name: str
    result: str = ""
    is_error: bool = False
    duration_ms: int = 0
    tool_result: ToolResult | None = None
    type: Literal["tool_call_end"] = "tool_call_end"
    tool_output: CanonicalToolOutput | None = None


@dataclass(slots=True)
class ToolProgressEvent:
    """Transient progress reported by an in-flight tool invocation.

    Progress is observational only: it is never added to provider history and
    does not alter the final :class:`ToolResult`. The optional ``data``
    dictionary is passed to observers without validating its contents.

    Attributes:
        data: Optional observer payload dictionary whose contents are not
            validated.

    Args:
        tool_use_id: Identifier assigned to the tool call.
        tool_name: Name of the in-flight tool.
        message: Human-readable progress message.
        data: Optional progress payload passed to observers.
        type: Wire discriminator for the event.
    """

    tool_use_id: str
    tool_name: str
    message: str
    data: dict[str, Any] | None = None
    type: Literal["tool_progress"] = "tool_progress"


@dataclass(slots=True)
class PermissionRequestItem:
    """Describe one tool call awaiting permission.

    Args:
        tool_use_id: Identifier assigned to the pending tool call.
        tool_name: Name of the tool requesting permission.
        input: JSON-compatible input proposed for the tool.
        summary: Human-readable summary shown to the approver.
    """

    tool_use_id: str
    tool_name: str
    input: dict[str, Any]
    summary: str


@dataclass(slots=True)
class PermissionRequestEvent:
    """Record a batch of tool calls awaiting permission.

    Args:
        requests: Pending tool-call descriptions.
        type: Wire discriminator for the event.
    """

    requests: list[PermissionRequestItem]
    type: Literal["permission_request"] = "permission_request"


@dataclass(slots=True)
class UsageEvent:
    """Record per-call and cumulative token usage.

    Attributes:
        usage: Usage reported for the current provider call.
        cumulative: Usage accumulated across the run.
        source: Optional label identifying the usage producer.
        call_id: Optional provider call identifier.
        logical_call_id: Optional identifier shared across retries.

    Args:
        usage: Usage reported for the current provider call.
        cumulative: Usage accumulated across the run.
        type: Wire discriminator for the event.
        cost_usd: Optional cost for the current call.
        cumulative_cost_usd: Optional cost accumulated across the run.
        source: Optional source label for the usage record.
        call_id: Optional provider call identifier.
        logical_call_id: Optional identifier shared across retries.
        model: Optional model that produced the usage.
    """

    usage: Usage
    cumulative: Usage
    type: Literal["usage"] = "usage"
    cost_usd: float | None = None
    """USD cost for the current turn, or ``None`` for unknown models."""
    cumulative_cost_usd: float | None = None
    """Accumulated USD cost across all turns so far, or ``None`` if no priced
    turn has run yet."""
    source: str | None = None
    call_id: str | None = None
    logical_call_id: str | None = None
    model: str | None = None


@dataclass(frozen=True, slots=True)
class ModelCallEvent:
    """Versioned accounting journal envelope with an isolated JSON record.

    Attributes:
        record_kind: Accounting transition represented by the record.
        budget_owner_id: Optional owner used to attribute budget consumption.
        schema_version: Wire schema version used to interpret the record.

    Args:
        run_id: Run associated with the accounting record.
        record_kind: Kind of accounting record.
        record: JSON record containing the accounting payload.
        budget_owner_id: Optional owner used for budget accounting.
        schema_version: Wire schema version for the record.
        type: Wire discriminator for the event.
    """

    run_id: str
    record_kind: Literal["intent", "dispatch_started", "receipt", "abandoned", "projection"]
    record: dict[str, Any]
    budget_owner_id: str | None = None
    schema_version: int = WIRE_VERSION
    type: Literal["model_call"] = "model_call"

    def __post_init__(self) -> None:
        from .model_calls import _copy_model_call_record, _invalid

        if self.type != "model_call":
            raise _invalid("model-call event has an invalid type")
        object.__setattr__(
            self,
            "record",
            _copy_model_call_record(
                self.run_id,
                self.record_kind,
                self.record,
                self.budget_owner_id,
                self.schema_version,
            ),
        )


@dataclass(slots=True)
class BudgetEvent:
    """Emitted when a :class:`~linch.budget.RunBudget` crosses its warning
    ratio (``kind="warning"``, once per budget object) or is exhausted
    (``kind="exceeded"``, after which the run stops with an error result).

    Args:
        kind: Budget transition represented by the event.
        spent_tokens: Tokens spent when the event was emitted.
        spent_usd: Cost spent when the event was emitted.
        max_tokens: Configured token limit, if any.
        max_cost_usd: Configured cost limit, if any.
        type: Wire discriminator for the event.
    """

    kind: Literal["warning", "exceeded"]
    spent_tokens: int
    spent_usd: float
    max_tokens: int | None
    max_cost_usd: float | None
    type: Literal["budget"] = "budget"


@dataclass(slots=True)
class ResultEvent:
    """Record the terminal outcome of a run.

    Attributes:
        structured_error: Parsing or validation error for structured output,
            when present.
        usage_complete: Whether all accounted model usage is known.
        trace_id: Optional trace correlation identifier for the run.

    Args:
        subtype: Terminal outcome category.
        stop_reason: Reason the loop stopped.
        total_usage: Usage accumulated across the run.
        duration_ms: Run duration in milliseconds.
        final_text: Optional final textual answer.
        structured_output: Parsed structured answer, when available.
        structured_error: Structured-output parsing or validation error.
        total_cost_usd: Optional total run cost.
        type: Wire discriminator for the event.
        usage_complete: Whether usage accounting is complete.
        trace_id: Optional trace identifier associated with the run.
    """

    subtype: Literal["success", "error", "aborted", "interrupted"]
    stop_reason: StopReason
    total_usage: Usage
    duration_ms: int
    final_text: str | None = None
    structured_output: dict[str, Any] | None = None
    """Parsed JSON output when an ``OutputSchema`` was configured.  ``None``
    when no schema was set or when parsing failed (check ``structured_error``
    for the failure reason)."""
    structured_error: str | None = None
    """Error message from JSON parsing / schema validation.  Set when
    ``output_schema`` was configured but the model's response was not valid
    JSON or did not match the schema."""
    total_cost_usd: float | None = None
    """Total USD cost across all turns, or ``None`` if the model is not in the
    pricing table.  Partial sums are possible for multi-model runs where only
    some turns have known pricing."""
    type: Literal["result"] = "result"
    usage_complete: bool = True
    trace_id: str | None = None


@dataclass(slots=True)
class ErrorEvent:
    """Record a terminal or recoverable error payload.

    Args:
        error: Structured error details.
        type: Wire discriminator for the event.
    """

    error: dict[str, Any]
    type: Literal["error"] = "error"


@dataclass(slots=True)
class CompactionEvent:
    """Record a context-compaction operation.

    Args:
        messages_before: Message count before compaction.
        messages_after: Message count after compaction.
        tokens_before: Token estimate before compaction.
        tokens_after: Token estimate after compaction.
        strategy: Compaction strategy used.
        type: Wire discriminator for the event.
    """

    messages_before: int
    messages_after: int
    tokens_before: int
    tokens_after: int
    strategy: str
    type: Literal["compaction"] = "compaction"


@dataclass(slots=True)
class ModelFallbackEvent:
    """Emitted when the active model is swapped after a provider overload.

    The swap is run-level: every subsequent turn uses ``to_model`` until the
    run ends or another overload escalates to the next fallback.

    Args:
        from_model: Model that became unavailable or overloaded.
        to_model: Fallback model selected for subsequent turns.
        reason: Optional reason for the fallback.
        type: Wire discriminator for the event.
    """

    from_model: str
    to_model: str
    reason: str = ""
    type: Literal["model_fallback"] = "model_fallback"


@dataclass(slots=True)
class ContextBuildEvent:
    """Record the context assembled for a provider request.

    Attributes:
        selected_tools: Tool names offered in this request, when recorded.
        budget: Context-budget accounting details used for the request.
        metadata: Additional context-builder metadata for observers.

    Args:
        system_blocks: Number of system blocks selected.
        messages: Number of conversation messages selected.
        selected_tools: Tool names offered in the request.
        budget: Context-budget accounting details.
        metadata: Additional context-builder metadata.
        type: Wire discriminator for the event.
    """

    system_blocks: int
    messages: int
    selected_tools: list[str] | None
    budget: dict[str, Any]
    metadata: dict[str, Any]
    type: Literal["context_build"] = "context_build"


@dataclass(slots=True)
class SkillsLoadedEvent:
    """Record the skills loaded for a request or session.

    Args:
        skills: Skill descriptors visible to the model.
        type: Wire discriminator for the event.
    """

    skills: list[dict[str, Any]]
    type: Literal["skills_loaded"] = "skills_loaded"


@dataclass(slots=True)
class SkillInvokedEvent:
    """Record a skill invocation.

    Attributes:
        allowed_tools: Optional tool names allowed by the invoked skill.
        model_override: Optional temporary model selected by the skill.

    Args:
        name: Invoked skill name.
        args: Optional argument text supplied to the skill.
        model_override: Optional model selected by the skill.
        allowed_tools: Optional tool allowlist selected by the skill.
        type: Wire discriminator for the event.
    """

    name: str
    args: str | None = None
    model_override: str | None = None
    allowed_tools: list[str] | None = None
    type: Literal["skill_invoked"] = "skill_invoked"


@dataclass(slots=True)
class SkillCompletedEvent:
    """Record completion of a skill invocation.

    Args:
        name: Completed skill name.
        is_error: Whether skill execution failed.
        type: Wire discriminator for the event.
    """

    name: str
    is_error: bool = False
    type: Literal["skill_completed"] = "skill_completed"


@dataclass(slots=True)
class SubagentEvent:
    """Wrap an event emitted by a subagent run.

    Attributes:
        parent_session_id: Session identifier used to correlate the child with
            its parent.
        subagent_run_id: Run identifier used to correlate nested events.
        event: The nested event emitted by the subagent.

    Args:
        parent_session_id: Session that spawned the subagent.
        subagent_run_id: Run identifier of the subagent.
        subagent_type: Subagent definition used for the run.
        display_name: Human-readable subagent name.
        event: Event emitted by the subagent.
        type: Wire discriminator for the event.
    """

    parent_session_id: str
    subagent_run_id: str
    subagent_type: str
    display_name: str
    event: Event
    type: Literal["subagent_event"] = "subagent_event"

    def __post_init__(self) -> None:
        _validate_nested_ignorable_event(self)


@dataclass(slots=True)
class BackgroundWorkerEvent:
    """Record a background subagent worker state transition.

    Args:
        worker_id: Identifier assigned to the worker.
        status: Worker state such as started, completed, failed, or aborted.
        display_name: Human-readable worker name.
        type: Wire discriminator for the event.
    """

    # The status string is intentionally open-ended for host-defined worker states.

    worker_id: str
    status: str  # "started" | "completed" | "failed" | "aborted" | "killed"
    display_name: str
    type: Literal["background_worker"] = "background_worker"


@dataclass(slots=True)
class LoopGuardEvent:
    """Emitted when the loop guard trips or when ``max_turns`` is reached.

    Attributes:
        reason: Machine-readable tag for the trip condition.  One of
            ``"repeated_tool_call"``, ``"repeated_failures"``, or
            ``"max_turns"``.
        detail: Human-readable description of why the guard tripped.
        action: What the loop did in response — ``"stop"`` (hard error
            termination) or ``"force_final"`` (one tools-disabled turn
            injected before stopping).

    Args:
        reason: Machine-readable trip condition.
        detail: Human-readable trip description.
        action: Loop action taken after the guard tripped.
        type: Wire discriminator for the event.
    """

    reason: str
    detail: str
    action: str
    type: Literal["loop_guard"] = "loop_guard"


@dataclass(slots=True)
class VerificationEvent:
    """Emitted when a closed-loop gate acts on a would-be-final answer.

    Attributes:
        verifier: Name of the gate — a custom verifier's ``name`` or
            ``"output_schema"`` for the built-in structured-output repair.
        action: ``"retry"`` (feedback injected, loop continues),
            ``"stop"`` (run fails), or ``"exhausted"`` (a retry verdict was
            returned but no retries remain; the answer is accepted as-is).
        feedback: The feedback or error message attached to the verdict.
        attempt: Retry attempts used so far in this run (1-based on the
            first retry; shared across all verifiers in the same run).

    Args:
        verifier: Name of the verifier that produced the action.
        action: Verification action taken.
        feedback: Feedback or error attached to the verdict.
        attempt: Retry attempt count at emission time.
        type: Wire discriminator for the event.
    """

    verifier: str
    action: str
    feedback: str = ""
    attempt: int = 0
    type: Literal["verification"] = "verification"


@dataclass(slots=True)
class HookEventRecord:
    """Record a hook dispatch decision.

    Args:
        event: Lifecycle event name.
        hook: Hook identifier.
        action: Action returned by the hook.
        reason: Optional explanation for the action.
        type: Wire discriminator for the event.
    """

    event: str
    hook: str
    action: str
    reason: str = ""
    type: Literal["hook"] = "hook"


@dataclass(slots=True)
class ScheduleEvent:
    """Describe a schedule firing for event consumers.

    Args:
        schedule_id: Schedule that fired.
        status: Firing status.
        payload: Payload associated with the firing.
        type: Wire discriminator for the event.
    """

    schedule_id: str
    status: str  # "fired"
    payload: str = ""
    type: Literal["schedule"] = "schedule"


WORKFLOW_EVENT_KINDS: tuple[str, ...] = (
    "phase",
    "agent_start",
    "agent_end",
    "agent_replayed",
    "step_start",
    "step_end",
    "step_replayed",
    "interrupt_requested",
    "interrupt_resolved",
    "interrupt_replayed",
)
"""Every ``WorkflowEvent.kind``, in one place for the decoder to validate against.

Keep in lockstep with the ``Literal`` on :class:`WorkflowEvent` below (which must
stay a literal for the type checker) and with ``JOURNALED_KINDS`` in
``workflow/journal.py``, which selects the subset that rebuilds the resume journal.
"""


@dataclass(slots=True)
class WorkflowEvent:
    """Progress/journal event emitted by the workflow engine.

    The ``*_end`` and ``*_replayed`` records double as the resume journal:
    persisted to the run store, they let an unchanged ``wf.agent`` / ``wf.step``
    call prefix replay cached results on resume. A step's value is carried as
    JSON in ``result_text``.

    Attributes:
        call_key: Stable workflow-call key used to correlate lifecycle records.
        occurrence: Occurrence number distinguishing repeated calls.
        result_text: JSON-serialized step result used for replay, when present.

    Args:
        kind: Workflow lifecycle kind.
        title: Human-readable event title.
        call_key: Stable key for the workflow call.
        occurrence: Occurrence number for repeated calls.
        subagent_type: Subagent type associated with the event.
        result_text: Serialized step result, when present.
        structured_output: Structured step result, when present.
        structured_error: Error from structured result handling.
        type: Wire discriminator for the event.
    """

    # Keep in lockstep with WORKFLOW_EVENT_KINDS above.
    kind: Literal[
        "phase",
        "agent_start",
        "agent_end",
        "agent_replayed",
        "step_start",
        "step_end",
        "step_replayed",
        "interrupt_requested",
        "interrupt_resolved",
        "interrupt_replayed",
    ]
    title: str = ""
    call_key: str = ""
    occurrence: int = 0
    subagent_type: str = ""
    result_text: str | None = None
    structured_output: dict[str, Any] | None = None
    structured_error: str | None = None
    type: Literal["workflow"] = "workflow"


@dataclass(slots=True)
class PromptCacheAdvisoryEvent:
    """Advisory that a prefix-breaking change was detected between provider calls.

    Providers cache the leading identical bytes of each request (``tools`` →
    ``system`` → ``messages``). When the tool set or model changes across turns
    of the same session, that cached prefix is invalidated and the next request
    starts cold. This event is observational only — Linch reports the risk but
    never rewrites the request; keeping the prefix stable is the embedder's call.

    Attributes:
        reason: ``"tool_set_changed"`` (the tools leading the prefix changed) or
            ``"model_changed"`` (the cache is keyed per model).
        detail: Human-readable description of what changed and why it matters.

    Args:
        reason: Prefix-breaking change that invalidated the cache.
        detail: Human-readable explanation of the change.
        type: Wire discriminator for the event.
    """

    reason: Literal["tool_set_changed", "model_changed"]
    detail: str
    type: Literal["prompt_cache_advisory"] = "prompt_cache_advisory"


@dataclass(frozen=True, slots=True)
class ReliabilityEvent:
    """Record a reliability policy action during a run.

    Args:
        mechanism: Reliability mechanism that acted.
        action: Action taken by the mechanism.
        reason: Explanation for the action.
        turn_index: Turn on which the action occurred.
        correction_count: Number of corrections used so far.
        type: Wire discriminator for the event.
    """

    mechanism: str
    action: str
    reason: str
    turn_index: int
    correction_count: int
    type: Literal["reliability"] = "reliability"


# Persistence encodes events with ``_json_safe(..., strict=False)``, which
# substitutes ``"<max-depth>"`` past this depth rather than raising. An
# ignorable event must round-trip verbatim, so it may not nest deeper than the
# durable codec preserves. ``run_store`` imports this as its own limit to keep
# the two provably equal.
IGNORABLE_MAX_DEPTH = 100

_KNOWN_EVENT_TYPES = frozenset(
    {
        "assistant",
        "background_worker",
        "budget",
        "compaction",
        "context_build",
        "error",
        "hook",
        "loop_guard",
        "model_fallback",
        "model_call",
        "partial_assistant",
        "permission_request",
        "prompt_cache_advisory",
        "reliability",
        "result",
        "schedule",
        "skill_completed",
        "skill_invoked",
        "skills_loaded",
        "subagent_event",
        "system",
        "tool_call_end",
        "tool_call_start",
        "tool_progress",
        "usage",
        "user",
        "verification",
        "workflow",
    }
)


def _validate_ignorable_event_envelope(
    raw: dict[str, Any], *, original_type: str | None = None, depth: int = 0
) -> str:
    _validate_ignorable_json_value(raw, path="event", depth=depth)
    typ = raw.get("type")
    if not isinstance(typ, str) or not typ:
        raise ValueError("ignorable event type must be a non-empty string")
    if raw.get("ignorable") is not True:
        raise ValueError("unknown event must declare ignorable as true")
    if typ in _KNOWN_EVENT_TYPES:
        raise ValueError(f"known event type cannot be wrapped as ignorable: {typ!r}")
    if original_type is not None and original_type != typ:
        raise ValueError(
            f"ignorable event original_type {original_type!r} does not match raw type {typ!r}"
        )
    return typ


def _validate_ignorable_json_value(
    value: Any, *, path: str, seen: set[int] | None = None, depth: int = 0
) -> None:
    """Reject values that cannot survive an ignorable event round trip.

    Ignorable events are intentionally opaque, so preserving arbitrary Python
    objects would make their forwarding behavior depend on the in-memory
    process.  Keep the envelope to the same strict JSON value domain used by
    the durable codec: object keys must be strings, floats must be finite,
    recursive containers are rejected, and nesting stays inside the depth the
    durable codec preserves verbatim.
    """

    walk_json(
        value,
        check_node=_check_ignorable_json_node,
        check_key=_check_ignorable_json_key,
        path=path,
        seen=seen,
        depth=depth,
        copy_containers=False,
    )


def _check_ignorable_json_node(value: Any, path: str, active: set[int], depth: int) -> int | None:
    if value is None or isinstance(value, str | bool | int):
        return None
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"ignorable event {path} must not contain NaN or Infinity")
        return None
    if not isinstance(value, list | dict):
        raise ValueError(
            f"ignorable event {path} must be strictly JSON-serializable; got {type(value).__name__}"
        )
    if id(value) in active:
        raise ValueError(f"ignorable event {path} must not contain reference cycles")
    if depth >= IGNORABLE_MAX_DEPTH:
        raise ValueError(
            f"ignorable event {path} exceeds maximum nesting depth {IGNORABLE_MAX_DEPTH}"
        )
    return depth + 1


def _check_ignorable_json_key(key: Any, path: str) -> None:
    if not isinstance(key, str):
        raise ValueError(f"ignorable event {path} must contain only string object keys")


def _copy_ignorable_event_envelope(
    raw: dict[str, Any], *, original_type: str | None = None
) -> dict[str, Any]:
    """Validate and isolate an opaque event envelope at an API boundary."""

    _validate_ignorable_event_envelope(raw, original_type=original_type)
    # Validation proves deepcopy cannot encounter an unsupported value or a
    # cycle.  Returning a fresh tree prevents both the producer's input and a
    # serialized output from sharing nested state with the sentinel.
    return deepcopy(raw)


@dataclass(frozen=True, slots=True)
class IgnorableEvent:
    """A durable event of a type this build does not know, kept for pass-through.

    When a run log written by a newer schema carries an event whose envelope has
    ``ignorable: true``, decoding preserves it verbatim as this sentinel instead
    of aborting the replay. It re-serializes to its original ``raw`` payload, so
    telemetry and forwarding carry it forward unchanged. An unknown event without
    the flag is still rejected — the flag is the producer's promise it is safe to
    skip. Ports dsh's "required-on-read unless ignorable" session-log rule.

    Attributes:
        original_type: Unknown wire discriminator preserved for re-serialization.
        raw: Validated, isolated original event envelope.

    Args:
        original_type: Unknown wire type preserved by the sentinel.
        raw: Original JSON event envelope.
    """

    original_type: str
    raw: dict[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "raw",
            _copy_ignorable_event_envelope(self.raw, original_type=self.original_type),
        )

    @property
    def type(self) -> str:
        """Return the original wire type for generic consumers such as reports.

        Returns:
            The event's original wire discriminator.
        """

        return self.original_type


Event: TypeAlias = (
    SystemEvent
    | UserEvent
    | AssistantEvent
    | PartialAssistantEvent
    | ToolCallStartEvent
    | ToolProgressEvent
    | ToolCallEndEvent
    | PermissionRequestEvent
    | UsageEvent
    | BudgetEvent
    | ModelCallEvent
    | CompactionEvent
    | ModelFallbackEvent
    | ContextBuildEvent
    | ResultEvent
    | ErrorEvent
    | SkillsLoadedEvent
    | SkillInvokedEvent
    | SkillCompletedEvent
    | SubagentEvent
    | BackgroundWorkerEvent
    | LoopGuardEvent
    | VerificationEvent
    | HookEventRecord
    | ScheduleEvent
    | WorkflowEvent
    | PromptCacheAdvisoryEvent
    | ReliabilityEvent
    | IgnorableEvent
)


def is_system_event(e: Event) -> bool:
    """Return whether an event is a system initialization event.

    Args:
        e: Event to inspect.

    Returns:
        Whether ``e.type`` identifies a system initialization event.
    """

    return e.type == "system"  # type: ignore[comparison-overlap]


def is_assistant_event(e: Event) -> bool:
    """Return whether an event contains a completed assistant response.

    Args:
        e: Event to inspect.

    Returns:
        Whether ``e.type`` identifies a completed assistant response.
    """

    return e.type == "assistant"  # type: ignore[comparison-overlap]


def is_user_event(e: Event) -> bool:
    """Return whether an event contains a user message.

    Args:
        e: Event to inspect.

    Returns:
        Whether ``e.type`` identifies a user message event.
    """

    return e.type == "user"  # type: ignore[comparison-overlap]


def is_partial_assistant_event(e: Event) -> bool:
    """Return whether an event contains a streamed assistant delta.

    Args:
        e: Event to inspect.

    Returns:
        Whether ``e.type`` identifies a streamed assistant delta.
    """

    return e.type == "partial_assistant"  # type: ignore[comparison-overlap]


def is_tool_call_start_event(e: Event) -> bool:
    """Return whether an event starts a tool call.

    Args:
        e: Event to inspect.

    Returns:
        Whether ``e.type`` identifies the start of a tool call.
    """

    return e.type == "tool_call_start"  # type: ignore[comparison-overlap]


def is_tool_call_end_event(e: Event) -> bool:
    """Return whether an event ends a tool call.

    Args:
        e: Event to inspect.

    Returns:
        Whether ``e.type`` identifies the end of a tool call.
    """

    return e.type == "tool_call_end"  # type: ignore[comparison-overlap]


def is_tool_progress_event(e: Event) -> bool:
    """Return whether an event reports tool progress.

    Args:
        e: Event to inspect.

    Returns:
        Whether ``e.type`` identifies tool progress.
    """

    return e.type == "tool_progress"  # type: ignore[comparison-overlap]


def is_permission_request_event(e: Event) -> bool:
    """Return whether an event requests tool permission.

    Args:
        e: Event to inspect.

    Returns:
        Whether ``e.type`` identifies a permission request.
    """

    return e.type == "permission_request"  # type: ignore[comparison-overlap]


def is_usage_event(e: Event) -> bool:
    """Return whether an event reports token usage.

    Args:
        e: Event to inspect.

    Returns:
        Whether ``e.type`` identifies a usage event.
    """

    return e.type == "usage"  # type: ignore[comparison-overlap]


def is_ignorable_event(e: Event) -> bool:
    """Return whether an event is an unknown pass-through sentinel.

    Args:
        e: Event to inspect.

    Returns:
        Whether ``e`` is an :class:`IgnorableEvent` instance.
    """

    return isinstance(e, IgnorableEvent)


def is_model_call_event(e: Event) -> bool:
    """Return whether an event records a model accounting operation.

    Args:
        e: Event to inspect.

    Returns:
        Whether ``e.type`` identifies a model accounting event.
    """

    return e.type == "model_call"  # type: ignore[comparison-overlap]


def is_budget_event(e: Event) -> bool:
    """Return whether an event records a budget transition.

    Args:
        e: Event to inspect.

    Returns:
        Whether ``e.type`` identifies a budget transition.
    """

    return e.type == "budget"  # type: ignore[comparison-overlap]


def is_compaction_event(e: Event) -> bool:
    """Return whether an event records context compaction.

    Args:
        e: Event to inspect.

    Returns:
        Whether ``e.type`` identifies context compaction.
    """

    return e.type == "compaction"  # type: ignore[comparison-overlap]


def is_context_build_event(e: Event) -> bool:
    """Return whether an event records context assembly.

    Args:
        e: Event to inspect.

    Returns:
        Whether ``e.type`` identifies context assembly.
    """

    return e.type == "context_build"  # type: ignore[comparison-overlap]


def is_result_event(e: Event) -> bool:
    """Return whether an event contains a terminal result.

    Args:
        e: Event to inspect.

    Returns:
        Whether ``e.type`` identifies a terminal result.
    """

    return e.type == "result"  # type: ignore[comparison-overlap]


def is_error_event(e: Event) -> bool:
    """Return whether an event records an error.

    Args:
        e: Event to inspect.

    Returns:
        Whether ``e.type`` identifies an error event.
    """

    return e.type == "error"  # type: ignore[comparison-overlap]


def is_skills_loaded_event(e: Event) -> bool:
    """Return whether an event lists loaded skills.

    Args:
        e: Event to inspect.

    Returns:
        Whether ``e.type`` identifies a loaded-skills event.
    """

    return e.type == "skills_loaded"  # type: ignore[comparison-overlap]


def is_skill_invoked_event(e: Event) -> bool:
    """Return whether an event records a skill invocation.

    Args:
        e: Event to inspect.

    Returns:
        Whether ``e.type`` identifies a skill invocation.
    """

    return e.type == "skill_invoked"  # type: ignore[comparison-overlap]


def is_skill_completed_event(e: Event) -> bool:
    """Return whether an event records skill completion.

    Args:
        e: Event to inspect.

    Returns:
        Whether ``e.type`` identifies skill completion.
    """

    return e.type == "skill_completed"  # type: ignore[comparison-overlap]


def is_subagent_event(e: Event) -> bool:
    """Return whether an event was emitted by a subagent.

    Args:
        e: Event to inspect.

    Returns:
        Whether ``e.type`` identifies a subagent event.
    """

    return e.type == "subagent_event"  # type: ignore[comparison-overlap]


def is_loop_guard_event(e: Event) -> bool:
    """Return whether an event records a loop-guard action.

    Args:
        e: Event to inspect.

    Returns:
        Whether ``e.type`` identifies a loop-guard action.
    """

    return e.type == "loop_guard"  # type: ignore[comparison-overlap]


def is_verification_event(e: Event) -> bool:
    """Return whether an event records a verification action.

    Args:
        e: Event to inspect.

    Returns:
        Whether ``e.type`` identifies a verification action.
    """

    return e.type == "verification"  # type: ignore[comparison-overlap]


def is_hook_event(e: Event) -> bool:
    """Return whether an event records hook dispatch.

    Args:
        e: Event to inspect.

    Returns:
        Whether ``e.type`` identifies hook dispatch.
    """

    return e.type == "hook"  # type: ignore[comparison-overlap]


def is_workflow_event(e: Event) -> bool:
    """Return whether an event records workflow progress.

    Args:
        e: Event to inspect.

    Returns:
        Whether ``e.type`` identifies workflow progress.
    """

    return e.type == "workflow"  # type: ignore[comparison-overlap]


def usage_to_dict(usage: Usage) -> dict[str, int]:
    """Serialize token usage to a compact mapping.

    Args:
        usage: Usage record to encode.

    Returns:
        Mapping of usage counters.
    """

    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_creation_tokens": usage.cache_creation_tokens,
    }


def usage_from_dict(raw: dict[str, Any]) -> Usage:
    """Deserialize token usage from a mapping.

    Args:
        raw: Mapping containing usage counters.

    Returns:
        Parsed usage record, defaulting missing counters to zero.
    """

    return Usage(
        input_tokens=int(raw.get("input_tokens", 0) or 0),
        output_tokens=int(raw.get("output_tokens", 0) or 0),
        cache_read_tokens=int(raw.get("cache_read_tokens", 0) or 0),
        cache_creation_tokens=int(raw.get("cache_creation_tokens", 0) or 0),
    )


def _validate_nested_ignorable_event(event: Event) -> None:
    depth = 0
    active: set[int] = set()
    while isinstance(event, SubagentEvent):
        identity = id(event)
        if identity in active:
            raise ValueError("subagent event must not contain reference cycles")
        active.add(identity)
        depth += 1
        event = event.event
    if isinstance(event, IgnorableEvent):
        _validate_ignorable_event_envelope(
            event.raw, original_type=event.original_type, depth=depth
        )


def _json_safe(value: Any, *, active: set[int] | None = None, depth: int = 0) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if not isinstance(value, list | tuple | dict):
        return str(value)
    if active is None:
        active = set()
    identity = id(value)
    if identity in active:
        return "<recursion>"
    if depth >= IGNORABLE_MAX_DEPTH:
        return "<max-depth>"
    active.add(identity)
    try:
        if isinstance(value, dict):
            return {
                str(key): _json_safe(item, active=active, depth=depth + 1)
                for key, item in value.items()
            }
        return [_json_safe(item, active=active, depth=depth + 1) for item in value]
    finally:
        active.remove(identity)


def _within_depth(value: Any, limit: int) -> bool:
    # Explicit bound: json's own recursion limit differs between Python versions.
    stack = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        if isinstance(current, dict):
            items: Any = current.values()
        elif isinstance(current, list | tuple):
            items = current
        else:
            continue
        if depth >= limit:
            return False
        stack.extend((item, depth + 1) for item in items)
    return True


def _is_json_serializable(value: Any) -> bool:
    if not _within_depth(value, IGNORABLE_MAX_DEPTH):
        return False
    try:
        json.dumps(value)
    except (TypeError, ValueError, RecursionError):
        return False
    return True


def citation_to_dict(citation: Citation) -> dict[str, Any]:
    """Serialize a tool citation to a JSON-compatible mapping.

    Args:
        citation: Citation to encode.

    Returns:
        Citation mapping with JSON-safe metadata.
    """

    return {
        "id": citation.id,
        "source": citation.source,
        "label": citation.label,
        "chunk": citation.chunk,
        "score": citation.score,
        "metadata": _json_safe(citation.metadata),
    }


def citation_from_dict(raw: dict[str, Any]) -> Citation:
    """Deserialize a citation mapping.

    Args:
        raw: Mapping containing citation fields.

    Returns:
        Parsed citation with defaults for missing optional fields.
    """

    return Citation(
        id=str(raw.get("id", "")),
        source=str(raw.get("source", "")),
        label=raw.get("label") if isinstance(raw.get("label"), str) else None,
        chunk=raw.get("chunk") if isinstance(raw.get("chunk"), str) else None,
        score=float(raw["score"]) if isinstance(raw.get("score"), int | float) else None,
        metadata=dict(raw.get("metadata", {})) if isinstance(raw.get("metadata"), dict) else {},
    )


def tool_result_to_dict(result: ToolResult) -> dict[str, Any]:
    """Serialize a tool result for event persistence.

    Args:
        result: Tool result to encode.

    Returns:
        Wire-shaped tool-result mapping.
    """

    out: dict[str, Any] = {
        "content": result.content,
        "summary": result.summary,
        "is_error": result.is_error,
        "metadata": _json_safe(result.metadata),
        "citations": [citation_to_dict(citation) for citation in result.citations],
        "duration_ms": result.duration_ms,
        "truncated": result.truncated,
    }
    if result.recovery_hint:
        out["recovery_hint"] = result.recovery_hint
    if result.attachments and all(_is_json_serializable(item) for item in result.attachments):
        out["attachments"] = result.attachments
    return out


def tool_result_from_dict(raw: dict[str, Any]) -> ToolResult:
    """Deserialize a persisted tool-result mapping.

    Args:
        raw: Mapping containing tool-result fields.

    Returns:
        Parsed tool result.
    """

    citations = []
    for item in raw.get("citations", []):
        if isinstance(item, dict):
            citations.append(citation_from_dict(item))
    attachments = raw.get("attachments", [])
    if not isinstance(attachments, list):
        attachments = []
    return ToolResult(
        content=str(raw.get("content", "")),
        summary=str(raw.get("summary", "")),
        is_error=bool(raw.get("is_error", False)),
        metadata=dict(raw.get("metadata", {})) if isinstance(raw.get("metadata"), dict) else {},
        citations=citations,
        attachments=attachments,
        duration_ms=int(raw.get("duration_ms", 0) or 0),
        truncated=bool(raw.get("truncated", False)),
        recovery_hint=str(raw.get("recovery_hint", "")),
    )


def tool_attachment_to_dict(attachment: ToolAttachment) -> dict[str, Any]:
    """Serialize a tool attachment to its canonical mapping.

    Args:
        attachment: Attachment to encode.

    Returns:
        Wire-shaped attachment mapping.
    """

    normalized = normalize_tool_output(ToolOutput(value=None, attachments=[attachment]))
    assert isinstance(normalized, ToolOutput)
    item = normalized.attachments[0]
    return {
        "reference": item.reference,
        "name": item.name,
        "media_type": item.media_type,
        "metadata": item.metadata,
    }


def tool_attachment_from_dict(raw: dict[str, Any]) -> ToolAttachment:
    """Deserialize a canonical tool attachment mapping.

    Args:
        raw: Mapping containing attachment fields.

    Returns:
        Normalized tool attachment.
    """

    attachment = ToolAttachment(
        reference=raw.get("reference"),
        name=raw.get("name"),
        media_type=raw.get("media_type"),
        metadata=raw.get("metadata", {}),
    )
    normalized = normalize_tool_output(ToolOutput(value=None, attachments=[attachment]))
    assert isinstance(normalized, ToolOutput)
    return normalized.attachments[0]


def tool_output_to_dict(output: CanonicalToolOutput) -> dict[str, Any]:
    """Encode canonical tool output without lossy JSON coercion.

    Args:
        output: Canonical tool output to encode.

    Returns:
        Wire-shaped success or error mapping.
    """

    normalized = normalize_tool_output(output)
    common = {
        "attachments": [tool_attachment_to_dict(item) for item in normalized.attachments],
        "metadata": normalized.metadata,
    }
    if isinstance(normalized, ToolOutputError):
        return {
            "kind": "error",
            "message": normalized.message,
            "code": normalized.code,
            "details": normalized.details,
            **common,
        }
    return {"kind": "success", "value": normalized.value, **common}


def tool_output_from_dict(raw: dict[str, Any]) -> CanonicalToolOutput:
    """Decode a canonical output, rejecting malformed attachments and values.

    Args:
        raw: Mapping containing a canonical output kind and payload.

    Returns:
        Normalized canonical tool output.

    Raises:
        ValueError: If attachments, the error message, normalized values, or
            output kind are malformed.
    """

    raw_attachments = raw.get("attachments", [])
    if not isinstance(raw_attachments, list):
        raise ValueError("tool_output.attachments must be a list")
    attachments = []
    for index, item in enumerate(raw_attachments):
        if not isinstance(item, dict):
            raise ValueError(f"tool_output.attachments[{index}] must be an object")
        attachments.append(tool_attachment_from_dict(item))
    metadata = raw.get("metadata", {})
    kind = raw.get("kind", "success")
    if kind == "error":
        message = raw.get("message")
        if not isinstance(message, str):
            raise ValueError("tool_output.message must be a string")
        output: CanonicalToolOutput = ToolOutputError(
            message=message,
            code=raw.get("code"),
            details=raw.get("details"),
            attachments=attachments,
            metadata=metadata,
        )
    elif kind == "success":
        output = ToolOutput(
            value=raw.get("value"),
            attachments=attachments,
            metadata=metadata,
        )
    else:
        raise ValueError(f"unknown tool_output kind {kind!r}")
    return normalize_tool_output(output)


def event_to_dict(event: Event) -> dict[str, Any]:
    """Serialize a typed event to its wire representation.

    Args:
        event: Event to encode.

    Returns:
        Wire-shaped event mapping.
    """
    if isinstance(event, IgnorableEvent):
        # Re-serialize verbatim so a pass-through event round-trips unchanged.
        return _copy_ignorable_event_envelope(event.raw, original_type=event.original_type)
    if isinstance(event, ModelCallEvent):
        from .model_calls import _copy_model_call_record

        return {
            "type": event.type,
            "run_id": event.run_id,
            "record_kind": event.record_kind,
            "record": _copy_model_call_record(
                event.run_id,
                event.record_kind,
                event.record,
                event.budget_owner_id,
                event.schema_version,
            ),
            "budget_owner_id": event.budget_owner_id,
            "schema_version": event.schema_version,
        }
    if isinstance(event, SystemEvent):
        out = {
            "type": event.type,
            "subtype": event.subtype,
            "session_id": event.session_id,
            "run_id": event.run_id,
            "model": event.model,
            "tools": list(event.tools),
            "permission_mode": event.permission_mode,
            "cwd": event.cwd,
        }
        if event.trace_id is not None:
            out["trace_id"] = event.trace_id
        return out
    if isinstance(event, UserEvent):
        return {
            "type": event.type,
            "subtype": event.subtype,
            "message": message_to_dict(event.message),
        }
    if isinstance(event, AssistantEvent):
        return {
            "type": event.type,
            "message": message_to_dict(event.message),
            "stop_reason": event.stop_reason,
        }
    if isinstance(event, PartialAssistantEvent):
        return {"type": event.type, "delta": event.delta}
    if isinstance(event, ToolCallStartEvent):
        return {
            "type": event.type,
            "tool_use_id": event.tool_use_id,
            "tool_name": event.tool_name,
            "input": event.input,
            "summary": event.summary,
        }
    if isinstance(event, ToolProgressEvent):
        return {
            "type": event.type,
            "tool_use_id": event.tool_use_id,
            "tool_name": event.tool_name,
            "message": event.message,
            "data": _json_safe(event.data),
        }
    if isinstance(event, ToolCallEndEvent):
        out = {
            "type": event.type,
            "tool_use_id": event.tool_use_id,
            "tool_name": event.tool_name,
            "result": event.result,
            "is_error": event.is_error,
            "duration_ms": event.duration_ms,
        }
        if event.tool_result is not None:
            out["tool_result"] = tool_result_to_dict(event.tool_result)
        if event.tool_output is not None:
            out["tool_output"] = tool_output_to_dict(event.tool_output)
        return out
    if isinstance(event, PermissionRequestEvent):
        return {
            "type": event.type,
            "requests": [
                {
                    "tool_use_id": req.tool_use_id,
                    "tool_name": req.tool_name,
                    "input": req.input,
                    "summary": req.summary,
                }
                for req in event.requests
            ],
        }
    if isinstance(event, UsageEvent):
        d_usage: dict[str, Any] = {
            "type": event.type,
            "usage": usage_to_dict(event.usage),
            "cumulative": usage_to_dict(event.cumulative),
        }
        if event.cost_usd is not None:
            d_usage["cost_usd"] = event.cost_usd
        if event.cumulative_cost_usd is not None:
            d_usage["cumulative_cost_usd"] = event.cumulative_cost_usd
        for name in ("source", "call_id", "logical_call_id", "model"):
            value = getattr(event, name)
            if value is not None:
                d_usage[name] = value
        return d_usage
    if isinstance(event, BudgetEvent):
        return {
            "type": event.type,
            "kind": event.kind,
            "spent_tokens": event.spent_tokens,
            "spent_usd": event.spent_usd,
            "max_tokens": event.max_tokens,
            "max_cost_usd": event.max_cost_usd,
        }
    if isinstance(event, CompactionEvent):
        return {
            "type": event.type,
            "messages_before": event.messages_before,
            "messages_after": event.messages_after,
            "tokens_before": event.tokens_before,
            "tokens_after": event.tokens_after,
            "strategy": event.strategy,
        }
    if isinstance(event, ModelFallbackEvent):
        return {
            "type": event.type,
            "from_model": event.from_model,
            "to_model": event.to_model,
            "reason": event.reason,
        }
    if isinstance(event, ScheduleEvent):
        return {
            "type": event.type,
            "schedule_id": event.schedule_id,
            "status": event.status,
            "payload": event.payload,
        }
    if isinstance(event, ContextBuildEvent):
        return {
            "type": event.type,
            "system_blocks": event.system_blocks,
            "messages": event.messages,
            "selected_tools": event.selected_tools,
            "budget": dict(event.budget),
            "metadata": dict(event.metadata),
        }
    if isinstance(event, ResultEvent):
        d: dict[str, Any] = {
            "type": event.type,
            "subtype": event.subtype,
            "stop_reason": event.stop_reason,
            "total_usage": usage_to_dict(event.total_usage),
            "duration_ms": event.duration_ms,
            "final_text": event.final_text,
        }
        if event.structured_output is not None:
            d["structured_output"] = event.structured_output
        if event.structured_error is not None:
            d["structured_error"] = event.structured_error
        if event.total_cost_usd is not None:
            d["total_cost_usd"] = event.total_cost_usd
        if not event.usage_complete:
            d["usage_complete"] = False
        if event.trace_id is not None:
            d["trace_id"] = event.trace_id
        return d
    if isinstance(event, ErrorEvent):
        return {"type": event.type, "error": event.error}
    if isinstance(event, SkillsLoadedEvent):
        return {"type": event.type, "skills": event.skills}
    if isinstance(event, SkillInvokedEvent):
        return {
            "type": event.type,
            "name": event.name,
            "args": event.args,
            "model_override": event.model_override,
            "allowed_tools": event.allowed_tools,
        }
    if isinstance(event, SkillCompletedEvent):
        return {"type": event.type, "name": event.name, "is_error": event.is_error}
    if isinstance(event, SubagentEvent):
        _validate_nested_ignorable_event(event)
        return {
            "type": event.type,
            "parent_session_id": event.parent_session_id,
            "subagent_run_id": event.subagent_run_id,
            "subagent_type": event.subagent_type,
            "display_name": event.display_name,
            "event": event_to_dict(event.event),
        }
    if isinstance(event, BackgroundWorkerEvent):
        return {
            "type": event.type,
            "worker_id": event.worker_id,
            "status": event.status,
            "display_name": event.display_name,
        }
    if isinstance(event, LoopGuardEvent):
        return {
            "type": event.type,
            "reason": event.reason,
            "detail": event.detail,
            "action": event.action,
        }
    if isinstance(event, VerificationEvent):
        return {
            "type": event.type,
            "verifier": event.verifier,
            "action": event.action,
            "feedback": event.feedback,
            "attempt": event.attempt,
        }
    if isinstance(event, HookEventRecord):
        return {
            "type": event.type,
            "event": event.event,
            "hook": event.hook,
            "action": event.action,
            "reason": event.reason,
        }
    if isinstance(event, PromptCacheAdvisoryEvent):
        return {
            "type": event.type,
            "reason": event.reason,
            "detail": event.detail,
        }
    if isinstance(event, ReliabilityEvent):
        return asdict(event)
    if isinstance(event, WorkflowEvent):
        d = {
            "type": event.type,
            "kind": event.kind,
            "title": event.title,
            "call_key": event.call_key,
            "occurrence": event.occurrence,
            "subagent_type": event.subagent_type,
            "result_text": event.result_text,
        }
        if event.structured_output is not None:
            d["structured_output"] = event.structured_output
        if event.structured_error is not None:
            d["structured_error"] = event.structured_error
        return d
    raise ValueError(f"unknown event type: {getattr(event, 'type', '<missing>')}")


def _stored_event_to_dict(event: Event) -> dict[str, Any]:
    """Encode an event with the persisted run-event wire stamp."""

    return {"schema_version": WIRE_VERSION, "event": event_to_dict(event)}


def _stored_event_from_dict(raw: dict[str, Any]) -> Event:
    """Decode a persisted event after checking its outer wire version.

    ``schema_version`` belongs to the stored-event envelope, not to ordinary
    transient event values. Keeping the event payload nested means an opaque
    ``IgnorableEvent`` may contain its own ``schema_version`` field without
    colliding with the outer durability stamp. Model-call events retain their
    own inner version as well.
    """

    if not isinstance(raw, dict):
        require_wire_version(None)
    require_wire_version(raw.get("schema_version"))
    payload = raw.get("event")
    if not isinstance(payload, dict):
        raise ValueError("stored event envelope must contain an event object")
    return event_from_dict(payload)


_USER_EVENT_SUBTYPES = ("prompt", "tool_result", "alignment", "notification")
_PROMPT_CACHE_ADVISORY_REASONS = ("tool_set_changed", "model_changed")
_RESULT_SUBTYPES = ("success", "error", "aborted", "interrupted")
_STOP_REASONS = (
    "end_turn",
    "tool_use",
    "max_tokens",
    "stop_sequence",
    "refusal",
    "error",
    "lease_lost",
    "interrupted",
)


def _required_discriminator(
    raw: dict[str, Any], field: str, allowed: tuple[str, ...], default: str
) -> str:
    """Decode a discriminator without silently changing future semantics."""

    value = raw.get(field, default)
    if value not in allowed:
        raise ValueError(f"unknown {field} {value!r}; expected one of {allowed!r}")
    return value


def _optional_usage_metadata(raw: dict[str, Any], name: str) -> str | None:
    value = raw.get(name)
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ValueError(f"usage {name} must be a nonblank string or null")
    return value


def _usage_complete(raw: dict[str, Any]) -> bool:
    value = raw.get("usage_complete", True)
    if type(value) is not bool:
        raise ValueError("result usage_complete must be a boolean")
    return value


def event_from_dict(raw: dict[str, Any]) -> Event:
    """Deserialize a wire mapping into a typed event.

    Args:
        raw: Event mapping containing a supported type discriminator.

    Returns:
        Parsed event instance.

    Raises:
        ValueError: If an unknown non-ignorable type or invalid discriminator is
            encountered, or if an explicitly ignorable envelope is malformed.
        RuntimeError: If a model-call record violates its accounting contract.
    """
    if not isinstance(raw, dict):
        raise ValueError("event must be an object")
    typ = raw.get("type")
    # Checked before known-type dispatch: the ignorable branch below is reached
    # only after every known type has returned, so a known type carrying the
    # flag would otherwise decode as itself and bypass envelope validation.
    if raw.get("ignorable") is True and typ in _KNOWN_EVENT_TYPES:
        raise ValueError(f"known event type cannot be wrapped as ignorable: {typ!r}")
    if typ == "model_call":
        return ModelCallEvent(
            run_id=cast(Any, raw.get("run_id")),
            record_kind=cast(Any, raw.get("record_kind")),
            record=cast(Any, raw.get("record")),
            budget_owner_id=raw.get("budget_owner_id"),
            schema_version=cast(Any, raw.get("schema_version")),
        )
    if typ == "system":
        return SystemEvent(
            session_id=str(raw.get("session_id", "")),
            run_id=str(raw.get("run_id", "")),
            model=str(raw.get("model", "")),
            tools=[str(t) for t in raw.get("tools", [])],
            permission_mode=str(raw.get("permission_mode", "")),
            cwd=str(raw.get("cwd", "")),
            subtype="init",
            trace_id=raw.get("trace_id") if isinstance(raw.get("trace_id"), str) else None,
        )
    if typ == "user":
        subtype = _required_discriminator(raw, "subtype", _USER_EVENT_SUBTYPES, "prompt")
        return UserEvent(
            message=message_from_dict(dict(raw.get("message", {}))),
            subtype=cast(Any, subtype),
        )
    if typ == "assistant":
        stop_reason = _required_discriminator(raw, "stop_reason", _STOP_REASONS, "error")
        return AssistantEvent(
            message=message_from_dict(dict(raw.get("message", {}))),
            stop_reason=cast(StopReason, stop_reason),
        )
    if typ == "partial_assistant":
        return PartialAssistantEvent(delta=dict(raw.get("delta", {})))
    if typ == "tool_call_start":
        return ToolCallStartEvent(
            tool_use_id=str(raw.get("tool_use_id", "")),
            tool_name=str(raw.get("tool_name", "")),
            input=dict(raw.get("input", {})),
            summary=str(raw.get("summary", "")),
        )
    if typ == "tool_progress":
        data = raw.get("data")
        return ToolProgressEvent(
            tool_use_id=str(raw.get("tool_use_id", "")),
            tool_name=str(raw.get("tool_name", "")),
            message=str(raw.get("message", "")),
            data=dict(data) if isinstance(data, dict) else None,
        )
    if typ == "tool_call_end":
        raw_tool_result = raw.get("tool_result")
        raw_tool_output = raw.get("tool_output")
        return ToolCallEndEvent(
            tool_use_id=str(raw.get("tool_use_id", "")),
            tool_name=str(raw.get("tool_name", "")),
            result=str(raw.get("result", "")),
            is_error=bool(raw.get("is_error", False)),
            duration_ms=int(raw.get("duration_ms", 0) or 0),
            tool_result=tool_result_from_dict(raw_tool_result)
            if isinstance(raw_tool_result, dict)
            else None,
            tool_output=tool_output_from_dict(raw_tool_output)
            if isinstance(raw_tool_output, dict)
            else None,
        )
    if typ == "permission_request":
        requests = []
        for req in raw.get("requests", []):
            if not isinstance(req, dict):
                continue
            requests.append(
                PermissionRequestItem(
                    tool_use_id=str(req.get("tool_use_id", "")),
                    tool_name=str(req.get("tool_name", "")),
                    input=dict(req.get("input", {})),
                    summary=str(req.get("summary", "")),
                )
            )
        return PermissionRequestEvent(requests=requests)
    if typ == "usage":
        _cost = raw.get("cost_usd")
        _cum_cost = raw.get("cumulative_cost_usd")
        return UsageEvent(
            usage=usage_from_dict(dict(raw.get("usage", {}))),
            cumulative=usage_from_dict(dict(raw.get("cumulative", {}))),
            cost_usd=float(_cost) if isinstance(_cost, (int, float)) else None,
            cumulative_cost_usd=float(_cum_cost) if isinstance(_cum_cost, (int, float)) else None,
            source=_optional_usage_metadata(raw, "source"),
            call_id=_optional_usage_metadata(raw, "call_id"),
            logical_call_id=_optional_usage_metadata(raw, "logical_call_id"),
            model=_optional_usage_metadata(raw, "model"),
        )
    if typ == "budget":
        _max_tokens = raw.get("max_tokens")
        _max_cost = raw.get("max_cost_usd")
        return BudgetEvent(
            kind=cast(
                Any, _required_discriminator(raw, "kind", ("warning", "exceeded"), "warning")
            ),
            spent_tokens=int(raw.get("spent_tokens", 0) or 0),
            spent_usd=float(raw.get("spent_usd", 0.0) or 0.0),
            max_tokens=int(_max_tokens) if isinstance(_max_tokens, int) else None,
            max_cost_usd=float(_max_cost) if isinstance(_max_cost, (int, float)) else None,
        )
    if typ == "compaction":
        return CompactionEvent(
            messages_before=int(raw.get("messages_before", 0) or 0),
            messages_after=int(raw.get("messages_after", 0) or 0),
            tokens_before=int(raw.get("tokens_before", 0) or 0),
            tokens_after=int(raw.get("tokens_after", 0) or 0),
            strategy=str(raw.get("strategy", "")),
        )
    if typ == "model_fallback":
        return ModelFallbackEvent(
            from_model=str(raw.get("from_model", "")),
            to_model=str(raw.get("to_model", "")),
            reason=str(raw.get("reason", "")),
        )
    if typ == "schedule":
        return ScheduleEvent(
            schedule_id=str(raw.get("schedule_id", "")),
            status=str(raw.get("status", "")),
            payload=str(raw.get("payload", "")),
        )
    if typ == "context_build":
        selected_raw = raw.get("selected_tools")
        return ContextBuildEvent(
            system_blocks=int(raw.get("system_blocks", 0) or 0),
            messages=int(raw.get("messages", 0) or 0),
            selected_tools=(
                [str(t) for t in selected_raw] if isinstance(selected_raw, list) else None
            ),
            budget=dict(raw.get("budget", {})),
            metadata=dict(raw.get("metadata", {})),
        )
    if typ == "result":
        so_raw = raw.get("structured_output")
        se_raw = raw.get("structured_error")
        _total_cost = raw.get("total_cost_usd")
        return ResultEvent(
            subtype=cast(Any, _required_discriminator(raw, "subtype", _RESULT_SUBTYPES, "error")),
            stop_reason=cast(
                StopReason, _required_discriminator(raw, "stop_reason", _STOP_REASONS, "error")
            ),
            total_usage=usage_from_dict(dict(raw.get("total_usage", {}))),
            duration_ms=int(raw.get("duration_ms", 0) or 0),
            final_text=raw.get("final_text") if isinstance(raw.get("final_text"), str) else None,
            structured_output=dict(so_raw) if isinstance(so_raw, dict) else None,
            structured_error=str(se_raw) if isinstance(se_raw, str) else None,
            total_cost_usd=float(_total_cost) if isinstance(_total_cost, (int, float)) else None,
            usage_complete=_usage_complete(raw),
            trace_id=raw.get("trace_id") if isinstance(raw.get("trace_id"), str) else None,
        )
    if typ == "error":
        return ErrorEvent(error=dict(raw.get("error", {})))
    if typ == "skills_loaded":
        return SkillsLoadedEvent(skills=list(raw.get("skills", [])))
    if typ == "skill_invoked":
        return SkillInvokedEvent(
            name=str(raw.get("name", "")),
            args=raw.get("args") if isinstance(raw.get("args"), str) else None,
            model_override=raw.get("model_override")
            if isinstance(raw.get("model_override"), str)
            else None,
            allowed_tools=[str(t) for t in raw.get("allowed_tools", [])]
            if isinstance(raw.get("allowed_tools"), list)
            else None,
        )
    if typ == "skill_completed":
        return SkillCompletedEvent(
            name=str(raw.get("name", "")),
            is_error=bool(raw.get("is_error", False)),
        )
    if typ == "subagent_event":
        nested = raw.get("event")
        if not isinstance(nested, dict):
            raise ValueError("subagent_event.event must be an object")
        return SubagentEvent(
            parent_session_id=str(raw.get("parent_session_id", "")),
            subagent_run_id=str(raw.get("subagent_run_id", "")),
            subagent_type=str(raw.get("subagent_type", "")),
            display_name=str(raw.get("display_name", "")),
            event=event_from_dict(nested),
        )
    if typ == "background_worker":
        return BackgroundWorkerEvent(
            worker_id=str(raw.get("worker_id", "")),
            status=str(raw.get("status", "")),
            display_name=str(raw.get("display_name", "")),
        )
    if typ == "loop_guard":
        return LoopGuardEvent(
            reason=str(raw.get("reason", "")),
            detail=str(raw.get("detail", "")),
            action=str(raw.get("action", "stop")),
        )
    if typ == "verification":
        return VerificationEvent(
            verifier=str(raw.get("verifier", "")),
            action=str(raw.get("action", "")),
            feedback=str(raw.get("feedback", "")),
            attempt=int(raw.get("attempt", 0) or 0),
        )
    if typ == "hook":
        return HookEventRecord(
            event=str(raw.get("event", "")),
            hook=str(raw.get("hook", "")),
            action=str(raw.get("action", "")),
            reason=str(raw.get("reason", "")),
        )
    if typ == "prompt_cache_advisory":
        _reason = _required_discriminator(
            raw, "reason", _PROMPT_CACHE_ADVISORY_REASONS, "tool_set_changed"
        )
        return PromptCacheAdvisoryEvent(
            reason=cast(Any, _reason),
            detail=str(raw.get("detail", "")),
        )
    if typ == "reliability":
        turn_index = raw.get("turn_index")
        correction_count = raw.get("correction_count")
        if type(turn_index) is not int or turn_index < 0:
            raise ValueError("reliability turn_index must be a non-negative integer")
        if type(correction_count) is not int or correction_count < 0:
            raise ValueError("reliability correction_count must be a non-negative integer")
        return ReliabilityEvent(
            mechanism=str(raw.get("mechanism", "")),
            action=str(raw.get("action", "")),
            reason=str(raw.get("reason", "")),
            turn_index=turn_index,
            correction_count=correction_count,
        )
    if typ == "workflow":
        _kind = _required_discriminator(raw, "kind", WORKFLOW_EVENT_KINDS, "phase")
        return WorkflowEvent(
            kind=cast(Any, _kind),
            title=str(raw.get("title", "")),
            call_key=str(raw.get("call_key", "")),
            occurrence=int(raw.get("occurrence", 0) or 0),
            subagent_type=str(raw.get("subagent_type", "")),
            result_text=(
                raw.get("result_text") if isinstance(raw.get("result_text"), str) else None
            ),
            structured_output=(
                dict(raw["structured_output"])
                if isinstance(raw.get("structured_output"), dict)
                else None
            ),
            structured_error=(
                raw.get("structured_error")
                if isinstance(raw.get("structured_error"), str)
                else None
            ),
        )
    if raw.get("ignorable") is True:
        # Forward-compat: only a well-formed newer-schema event whose producer
        # explicitly promised it is safe to skip may survive as a sentinel.
        original_type = _validate_ignorable_event_envelope(raw)
        return IgnorableEvent(original_type=original_type, raw=raw)
    raise ValueError(f"unknown event type: {typ!r}")
