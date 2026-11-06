"""Run events: one frozen dataclass per kind the loop emits.

Nested values (messages, usage, tool result blocks) stay as the JSON dicts
the server sends; their shapes are documented in the `linch-core` crate.
Any other kind decodes to `UnknownEvent`, which keeps the raw payload.
Decoding is tolerant: members this client does not know are ignored.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any, ClassVar

JsonObject = dict[str, Any]


@dataclass(frozen=True)
class InitEvent:
    """The run started: its session, model and tool names."""

    type: ClassVar[str] = "init"
    session_id: str
    run_id: str
    model: str
    tools: list[str]
    parent_run_id: str | None = None
    trace_id: str | None = None


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
    """Token usage of one provider call, and the run's running total."""

    type: ClassVar[str] = "usage"
    usage: JsonObject
    cumulative: JsonObject


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
    """A tool call finished; `result` is the tool result block the model reads."""

    type: ClassVar[str] = "tool_call_end"
    tool_name: str
    result: JsonObject
    duration_ms: int
    canonical_output: Any = None

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
class CompactionEvent:
    """The provider view was compacted into a summary."""

    type: ClassVar[str] = "compaction"
    messages_before: int
    messages_after: int
    tokens_before: int
    tokens_after: int
    strategy: str


@dataclass(frozen=True)
class LoopGuardEvent:
    """The loop guard tripped, or `max_turns` was reached."""

    type: ClassVar[str] = "loop_guard"
    reason: str
    detail: str
    action: str


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
    """The run finished; `subtype` is `success`, `error`, `aborted` or `interrupted`."""

    type: ClassVar[str] = "result"
    subtype: str
    stop_reason: str
    total_usage: JsonObject
    usage_complete: bool
    final_text: str | None = None
    structured_output: JsonObject | None = None
    structured_error: str | None = None
    trace_id: str | None = None


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
    | CompactionEvent
    | LoopGuardEvent
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
        CompactionEvent,
        LoopGuardEvent,
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
        event: The decoded event.
    """

    seq: int
    run_id: str
    event: Event


def decode_event(raw: JsonObject) -> Event:
    """Decode one event object `{"type": ..., ...}`."""
    payload = {key: value for key, value in raw.items() if key != "type"}
    kind = str(raw.get("type", ""))
    cls = _KINDS.get(kind)
    if cls is None:
        return UnknownEvent(kind, payload)
    names = {field.name for field in fields(cls)}
    event: Event = cls(**{key: value for key, value in payload.items() if key in names})
    return event


def decode_envelope(raw: JsonObject) -> Envelope:
    """Decode one envelope `{"v", "seq", "run_id", "event", "ignorable"?}`."""
    return Envelope(int(raw["seq"]), str(raw["run_id"]), decode_event(raw["event"]))
