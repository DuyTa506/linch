"""Run events: one frozen dataclass per kind the loop emits.

Nested values (messages, usage, tool result blocks) stay as the JSON dicts
the server sends; their shapes are documented in the `linch-core` crate.
The child event a `subagent` event wraps is decoded like any other. Any
other kind decodes to `UnknownEvent`, which keeps the raw payload.
Decoding is tolerant: members this client does not know are ignored.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any, ClassVar

JsonObject = dict[str, Any]


@dataclass(frozen=True)
class InitEvent:
    """A call of the run started: its session, model, provider id and tools.

    A new run starts with one; each resume or approve starts with another,
    `resumed` and naming the `turn` it continues in.
    """

    type: ClassVar[str] = "init"
    session_id: str
    run_id: str
    model: str
    provider: str
    tools: list[str]
    parent_run_id: str | None = None
    trace_id: str | None = None
    resumed: bool = False
    turn: int = 0


@dataclass(frozen=True)
class UserEvent:
    """A user message: the prompt, tool results, or a runtime notification."""

    type: ClassVar[str] = "user"
    message: JsonObject
    subtype: str


@dataclass(frozen=True)
class AssistantEvent:
    """A completed assistant response."""

    type: ClassVar[str] = "assistant"
    message: JsonObject
    stop_reason: str


@dataclass(frozen=True)
class PartialAssistantEvent:
    """A streaming delta of the response. Stream-only: never replayed."""

    type: ClassVar[str] = "partial_assistant"
    delta: JsonObject


@dataclass(frozen=True)
class UsageEvent:
    """Token usage of one provider call, and the run's running total.

    `cost_usd` and `cumulative_cost_usd` are in USD, `None` when the model
    has no price. `actual_model` is the model the provider reported
    serving the call, `None` when it named none.
    """

    type: ClassVar[str] = "usage"
    usage: JsonObject
    cumulative: JsonObject
    cost_usd: float | None = None
    cumulative_cost_usd: float | None = None
    actual_model: str | None = None


@dataclass(frozen=True)
class ToolCallStartEvent:
    """A tool call is about to run."""

    type: ClassVar[str] = "tool_call_start"
    tool_use_id: str
    tool_name: str
    input: JsonObject
    summary: str


@dataclass(frozen=True)
class ToolCallEndEvent:
    """A tool call finished; `result` is the tool result block the model reads.

    `metadata` is what the tool returned for the host only, never shown to
    the model (a host tool's `ToolOutput.metadata`, or where an offloaded
    result went); `None` for an error or a tool that set none. `error_code`
    is a failed call's machine-readable code, `None` without one.
    """

    type: ClassVar[str] = "tool_call_end"
    tool_name: str
    result: JsonObject
    duration_ms: int
    canonical_output: Any = None
    metadata: JsonObject | None = None
    error_code: str | None = None

    @property
    def tool_use_id(self) -> str:
        """The call this result answers."""
        return str(self.result.get("tool_use_id", ""))

    @property
    def is_error(self) -> bool:
        """Whether the result is a tool error."""
        return bool(self.result.get("is_error", False))


@dataclass(frozen=True)
class ToolProgressEvent:
    """Progress from a running tool. Stream-only: never replayed."""

    type: ClassVar[str] = "tool_progress"
    tool_use_id: str
    tool_name: str
    message: str
    data: JsonObject | None = None


@dataclass(frozen=True)
class PermissionRequestEvent:
    """Tool calls that need the host's approval."""

    type: ClassVar[str] = "permission_request"
    requests: list[JsonObject]


@dataclass(frozen=True)
class SuspendedEvent:
    """The run stopped to wait; answer `pending_request_ids` with `Agent.approve`."""

    type: ClassVar[str] = "suspended"
    status: str
    pending_request_ids: list[str]


@dataclass(frozen=True)
class ContextBuildEvent:
    """The context of the next provider request was assembled; the call starts."""

    type: ClassVar[str] = "context_build"
    model: str
    system_blocks: int
    messages: int
    budget: JsonObject
    metadata: JsonObject
    selected_tools: list[str] | None = None


@dataclass(frozen=True)
class CompactionEvent:
    """The provider view was compacted into a summary, in `duration_ms`."""

    type: ClassVar[str] = "compaction"
    messages_before: int
    messages_after: int
    tokens_before: int
    tokens_after: int
    strategy: str
    duration_ms: int


@dataclass(frozen=True)
class LoopGuardEvent:
    """The loop guard tripped, or `max_turns` was reached."""

    type: ClassVar[str] = "loop_guard"
    reason: str
    detail: str
    action: str


@dataclass(frozen=True)
class SkillInvokedEvent:
    """The `Skill` tool loaded a skill, with the arguments it was given."""

    type: ClassVar[str] = "skill_invoked"
    name: str
    args: str | None = None
    model_override: str | None = None
    allowed_tools: list[str] | None = None


@dataclass(frozen=True)
class SkillCompletedEvent:
    """That skill's call ended; `is_error` when the skill failed to load."""

    type: ClassVar[str] = "skill_completed"
    name: str
    is_error: bool = False


@dataclass(frozen=True)
class SubagentEvent:
    """An event of a subagent's child run, decoded like the parent's own.

    `tool_use_id` is the parent's call that runs the child, and `ts_ms` is
    when the child emitted the event: a background child's events reach the
    parent's log later.
    """

    type: ClassVar[str] = "subagent"
    child_run_id: str
    tool_use_id: str
    ts_ms: int
    event: Event


@dataclass(frozen=True)
class ChildRunStatusEvent:
    """A background worker's child run changed status (`running`, `completed`, ...)."""

    type: ClassVar[str] = "child_run_status"
    child_run_id: str
    status: str


@dataclass(frozen=True)
class HookEvent:
    """One intercept-callback or hook call and its outcome. Stream-only."""

    type: ClassVar[str] = "hook"
    point: str
    policy_id: str
    policy_version: int
    outcome: JsonObject
    tool_use_id: str | None = None


@dataclass(frozen=True)
class ErrorEvent:
    """An error the run recorded, such as a provider failure."""

    type: ClassVar[str] = "error"
    error: JsonObject


@dataclass(frozen=True)
class ResultEvent:
    """The run finished; `subtype` is `success`, `error`, `aborted` or `interrupted`.

    `duration_ms` is the wall time of the start, resume or approve call that
    ended the run. `total_cost_usd` is what its priced calls cost in USD,
    `None` when none was priced.
    """

    type: ClassVar[str] = "result"
    subtype: str
    stop_reason: str
    total_usage: JsonObject
    usage_complete: bool
    duration_ms: int
    final_text: str | None = None
    structured_output: JsonObject | None = None
    structured_error: str | None = None
    trace_id: str | None = None
    total_cost_usd: float | None = None


@dataclass(frozen=True)
class UnknownEvent:
    """A kind this client has no class for; `raw` is the payload without `type`."""

    type: str
    raw: JsonObject


Event = (
    InitEvent
    | UserEvent
    | AssistantEvent
    | PartialAssistantEvent
    | UsageEvent
    | ToolCallStartEvent
    | ToolCallEndEvent
    | ToolProgressEvent
    | PermissionRequestEvent
    | SuspendedEvent
    | ContextBuildEvent
    | CompactionEvent
    | LoopGuardEvent
    | SkillInvokedEvent
    | SkillCompletedEvent
    | SubagentEvent
    | ChildRunStatusEvent
    | HookEvent
    | ErrorEvent
    | ResultEvent
    | UnknownEvent
)

_KINDS: dict[str, type[Any]] = {
    cls.type: cls
    for cls in (
        InitEvent,
        UserEvent,
        AssistantEvent,
        PartialAssistantEvent,
        UsageEvent,
        ToolCallStartEvent,
        ToolCallEndEvent,
        ToolProgressEvent,
        PermissionRequestEvent,
        SuspendedEvent,
        ContextBuildEvent,
        CompactionEvent,
        LoopGuardEvent,
        SkillInvokedEvent,
        SkillCompletedEvent,
        SubagentEvent,
        ChildRunStatusEvent,
        HookEvent,
        ErrorEvent,
        ResultEvent,
    )
}


@dataclass(frozen=True)
class Envelope:
    """One event of a run, as the server sends it.

    Attributes:
        seq: The run's durable sequence number; a stream-only event carries
            the last durable seq. Replay with `Agent.events(run_id, after_seq=seq)`.
        run_id: The run the event belongs to.
        ts_ms: When the server made the envelope, in Unix milliseconds.
        event: The decoded event.
    """

    seq: int
    run_id: str
    ts_ms: int
    event: Event


# Each kind's field names (`type` is a class variable, not among them).
_FIELDS: dict[str, frozenset[str]] = {
    kind: frozenset(field.name for field in fields(cls)) for kind, cls in _KINDS.items()
}


def decode_event(raw: JsonObject) -> Event:
    """Decode one event object `{"type": ..., ...}`."""
    kind = str(raw.get("type", ""))
    cls = _KINDS.get(kind)
    if cls is None:
        return UnknownEvent(kind, {key: value for key, value in raw.items() if key != "type"})
    if cls is SubagentEvent:
        return SubagentEvent(
            child_run_id=str(raw.get("child_run_id", "")),
            tool_use_id=str(raw.get("tool_use_id", "")),
            ts_ms=int(raw.get("ts_ms", 0)),
            event=decode_event(raw.get("event", {})),
        )
    names = _FIELDS[kind]
    event: Event = cls(**{key: value for key, value in raw.items() if key in names})
    return event


def decode_envelope(raw: JsonObject) -> Envelope:
    """Decode one envelope `{"v", "seq", "run_id", "ts_ms", "event", "ignorable"?}`."""
    return Envelope(
        seq=int(raw["seq"]),
        run_id=str(raw["run_id"]),
        ts_ms=int(raw["ts_ms"]),
        event=decode_event(raw["event"]),
    )
