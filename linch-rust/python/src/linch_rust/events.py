"""Run events: one frozen dataclass per kind the loop emits.

Nested values (messages, usage, tool result blocks) stay as the JSON dicts
the server sends; their shapes are documented in the `linch-core` crate.
The child event a `subagent` event wraps is decoded like any other. Any
other kind decodes to `UnknownEvent`, which keeps the raw payload.
Decoding is tolerant: members this client does not know are ignored.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any, ClassVar

from .errors import LinchError

JsonObject = dict[str, Any]
# The envelope `v` this client reads, as `WIRE_VERSION` in linch-core.
WIRE_VERSION = 1


@dataclass(frozen=True)
class InitEvent:
    """A call of the run started: its session, model, provider id and tools.

    A new run starts with one; each resume or approve starts with another,
    `resumed` and naming the `turn` it continues in. A run started with its
    own `output_schema` or `tool_choice` has it on its first `init`, and a
    run with a final tool names it there as `final_tool`; `tools` lists the
    final tool too when the run made it from its output schema.
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
    output_schema: JsonObject | None = None
    tool_choice: JsonObject | None = None
    final_tool: str | None = None


@dataclass(frozen=True)
class ScheduleFireEvent:
    """The schedule fire that started the run, right after its `init`.

    Only a run a worker started for a schedule has one; it is durable, so
    `Agent.events` finds it. `fire_at_ms` is when the fire was due (Unix
    milliseconds), not when the worker got to it. A resume of the run does
    not repeat it.
    """

    type: ClassVar[str] = "schedule_fire"
    schedule_id: str
    fire_at_ms: int


@dataclass(frozen=True)
class UserEvent:
    """A user message: the prompt, tool results, a runtime notification, or a steer.

    A steer (`subtype` `"alignment"`) is the text a `steer` call sent, as the
    model read it, and `steer_id` is the id that call returned. It is a user
    message of its own, after the tool results of the batch it came in, or
    after a text answer the steer kept from ending the run.
    """

    type: ClassVar[str] = "user"
    message: JsonObject
    subtype: str
    steer_id: str | None = None


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
    result went; for a failed call, the error's `details` object, such as a
    `Bash` command's `status` and `exit_code`); `None` for a tool that set
    none. `error_code` is a failed call's machine-readable code, `None`
    without one; `tool_interrupted` marks the stand-in end of a call that a
    crash interrupted after its start: its effect is unknown and it is not
    run again, so reconcile it with `ToolContext.idempotency_key`.
    `cache_hit` is set when `spec.tool_cache` served the result: the tool
    did not run, and the call has no durable start.
    """

    type: ClassVar[str] = "tool_call_end"
    tool_name: str
    result: JsonObject
    duration_ms: int
    canonical_output: Any = None
    metadata: JsonObject | None = None
    error_code: str | None = None
    cache_hit: bool = False

    @property
    def tool_use_id(self) -> str:
        """The call this result answers."""
        return str(self.result.get("tool_use_id", ""))

    @property
    def is_error(self) -> bool:
        """Whether the result is a tool error."""
        return bool(self.result.get("is_error", False))


@dataclass(frozen=True)
class ToolInterruptedEvent:
    """A call a crash interrupted: it started, its end was never durable.

    Written by the resume that finds it, with the `ToolCallEndEvent` (an
    error whose `error_code` is `tool_interrupted`) that stands in for the
    lost end. The tool's effect is unknown and the call is not run again:
    reconcile it, keyed by `tool_use_id` (the same after the resume; see
    `ToolContext.idempotency_key`).
    """

    type: ClassVar[str] = "tool_interrupted"
    tool_use_id: str
    tool_name: str


@dataclass(frozen=True)
class ToolRetryEvent:
    """A tool attempt failed with an error `spec.tool_retry` retries.

    It is durable before the wait and the next attempt: `attempt` is the
    one about to run (from 2) of at most `max_attempts`, `error_class` the
    failure's class (the error's `code`, or `timeout`), `error` its message
    and `delay_ms` the wait. A crash after it leaves the call with no end,
    which a resume reports as interrupted and does not run again.
    """

    type: ClassVar[str] = "tool_retry"
    tool_use_id: str
    tool_name: str
    attempt: int
    max_attempts: int
    error_class: str
    error: str
    delay_ms: int


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
class InputRequestEvent:
    """The run asks for input (`ask_user` or `workflow_interrupt`); `request_id` names it.

    For `ask_user`, `request_id` is the `AskUser` call's `tool_use_id`,
    `prompt` holds the questions one per line, and `questions` the
    questions with their options (`{id, header, question, options:
    [{label, description}], multi_select}`). Answer with `Agent.answer`.
    """

    type: ClassVar[str] = "input_request"
    request_id: str
    kind: str
    prompt: str
    questions: list[JsonObject] = field(default_factory=list[JsonObject])


@dataclass(frozen=True)
class SuspendedEvent:
    """The run stopped to wait; answer `pending_request_ids` with `Agent.approve`,
    or with `Agent.answer` when `status` is `awaiting_input`."""

    type: ClassVar[str] = "suspended"
    status: str
    pending_request_ids: list[str]


@dataclass(frozen=True)
class DroppedContext:
    """One block the context budget dropped from a request.

    Attributes:
        source: The `policy_id` of the callback or hook that added it.
        index: Its position among that callback's blocks, from 0; the same
            index as in what the callback put in its metadata, so a
            retrieval callback's ids can be reconciled with what was sent.
        tokens: Its estimated tokens.
    """

    source: str
    index: int
    tokens: int


@dataclass(frozen=True)
class ContextBuildEvent:
    """The context of the next provider request was assembled; the call starts.

    `budget` is empty unless `spec.context_budget` is set; then it holds
    `max_tokens`, `used_tokens`, `remaining_tokens`, `trimmed`, `dropped`
    and, when pinned blocks alone are over the cap, `over_by`. The decision
    is made again for every request and is not stored for recovery.
    """

    type: ClassVar[str] = "context_build"
    model: str
    system_blocks: int
    messages: int
    budget: JsonObject
    metadata: JsonObject
    selected_tools: list[str] | None = None

    @property
    def dropped(self) -> list[DroppedContext]:
        """The blocks the context budget dropped from this request, in order."""
        return [
            DroppedContext(str(item["source"]), int(item["index"]), int(item["tokens"]))
            for item in self.budget.get("dropped", [])
        ]


@dataclass(frozen=True)
class CompactionEvent:
    """The provider view was compacted, in `duration_ms`.

    `strategy` is `summary`, `micro` when old tool results were cleared with
    no model call, or `clip` when a refused prompt's last turn had its tool
    results clipped to a budget (head and tail kept, the session's log whole).
    `forced` is true when the provider's refusal of a
    prompt as too long caused it, rather than the view reaching the trigger.
    `rejected` is set when a summary was not applied (the counts then show an
    unchanged view): a code, `tool_call`, `empty`, `context_length` (a forced
    summary request was refused as too long; a smaller one follows) or
    `not_smaller` (the view with
    the whole checkpoint in it is estimated no smaller than the view now), then what follows (the
    next due turn asks again, asks again once the view has grown, or the run ends).
    `measured_tokens` is what the trigger measured the view at when that was
    more than `tokens_before`: the provider's last reported prompt tokens plus
    the estimate of what followed them; `None` until a provider has reported usage.
    `clipped_bytes` is the bytes clipped out of text to make a prompt fit (what a
    forced summary request read less of the older part, or what a `clip` took
    out of the view); `None` when nothing was clipped.
    """

    type: ClassVar[str] = "compaction"
    messages_before: int
    messages_after: int
    tokens_before: int
    tokens_after: int
    strategy: str
    duration_ms: int
    forced: bool = False
    rejected: str | None = None
    measured_tokens: int | None = None
    clipped_bytes: int | None = None


@dataclass(frozen=True)
class LoopGuardEvent:
    """The loop guard tripped, or `max_turns` was reached.

    `reason` is `repeated_tool_call`, `repeated_failures`, `idle_turns`
    (turns in a row without a provider call; see `LoopGuard`) or
    `max_turns`; `action` is `stop`.
    """

    type: ClassVar[str] = "loop_guard"
    reason: str
    detail: str
    action: str


@dataclass(frozen=True)
class BudgetEvent:
    """A run budget crossed its warning ratio (`warning`) or ran out (`exceeded`)."""

    type: ClassVar[str] = "budget"
    kind: str
    spent_tokens: int
    spent_usd: float
    max_tokens: int | None = None
    max_cost_usd: float | None = None


@dataclass(frozen=True)
class ProviderRetryEvent:
    """A provider call failed with a retryable error; the run calls `model` again.

    It waits `delay_ms` first. `retry` counts this model's retries in the
    turn, from 1. `error` is the failure in an `ErrorEvent`'s shape (`kind`,
    `message`, `retryable`, `status` for an HTTP status). The deltas the
    failed call streamed (`PartialAssistantEvent`s before this event) are
    void: drop the partial text received so far, because the next call
    answers from the start and streams the whole answer again.
    """

    type: ClassVar[str] = "provider_retry"
    model: str
    retry: int
    delay_ms: int
    error: JsonObject


@dataclass(frozen=True)
class ModelFallbackEvent:
    """The run gave up on `from_model` and calls `to_model` for the rest of the run.

    `error` is the failure that ended `from_model`'s calls, in an
    `ErrorEvent`'s shape. The deltas the failed call streamed are void, as
    for a `ProviderRetryEvent`: drop the partial text received since the
    last such event, and `to_model` answers from the start.
    """

    type: ClassVar[str] = "model_fallback"
    from_model: str
    to_model: str
    error: JsonObject


@dataclass(frozen=True)
class PromptCacheAdvisoryEvent:
    """A change broke the provider's prompt cache (`tool_set_changed`, `model_changed`)."""

    type: ClassVar[str] = "prompt_cache_advisory"
    reason: str
    detail: str


@dataclass(frozen=True)
class VerificationEvent:
    """A closed-loop gate acted on a would-be-final answer (`retry`, `stop`, `exhausted`)."""

    type: ClassVar[str] = "verification"
    verifier: str
    action: str
    feedback: str = ""
    attempt: int = 0


@dataclass(frozen=True)
class PolicyEvent:
    """The reliability policy (`spec.policy`, see `Policy`) decided.

    `rule` is `prerequisite` (a call of `tool`, `tool_use_id`, was answered
    with an error and not run, because the tools in `missing` have not
    succeeded yet; `action` is `refuse`) or `completion` (a final answer
    failed the `gates`; `action` is `correct` while the run's corrections
    last, then `stop`, which ends the run failed with an `ErrorEvent` named
    `PolicyGateError`). `attempt` counts the corrections spent once this
    event is counted. `detail` is what the model was told.
    """

    type: ClassVar[str] = "policy"
    rule: str
    action: str
    detail: str
    tool: str | None = None
    tool_use_id: str | None = None
    gates: list[str] = field(default_factory=list[str])
    missing: list[str] = field(default_factory=list[str])
    attempt: int = 0


@dataclass(frozen=True)
class SkillsLoadedEvent:
    """The skills the model can load: one descriptor dict per skill."""

    type: ClassVar[str] = "skills_loaded"
    skills: list[JsonObject]


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
class WorkflowEvent:
    """A workflow engine record: a phase, a step's start, end or replay, or an interrupt."""

    type: ClassVar[str] = "workflow"
    kind: str
    title: str = ""
    call_key: str = ""
    occurrence: int = 0
    subagent_type: str = ""
    result_text: str | None = None
    structured_output: JsonObject | None = None
    structured_error: str | None = None


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
    `None` when none was priced. With an output schema, `structured_output`
    is the answer parsed and checked against it; when the answer failed
    the schema the run ends `error` with `structured_error` saying why.
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
class SteerReceivedEvent:
    """A steer the run accepted, before it reached the model.

    The server writes it to the journal and never streams it, so a client
    sees it only in envelopes read from a journal. The `user` event with
    `subtype` `"alignment"` and the same `steer_id` marks it delivered.

    Attributes:
        steer_id: The id the `steer` call returned.
        text: The steer's text, after prompt redaction.
    """

    type: ClassVar[str] = "steer_received"
    steer_id: str
    text: str


@dataclass(frozen=True)
class ModelCallDeliveryEvent:
    """One delivery attempt of a model call was dispatched.

    The three `model_call_*` kinds are the accounting journal's records:
    the server writes them to the journal and never streams them, so a
    client sees them only in envelopes read from a journal.

    Attributes:
        logical_call_id: The logical call this attempts; its receipts share it.
        delivery_attempt_id: This attempt, the unit of deduplication.
        kind: `primary` (a turn's call) or `compaction`.
        turn_index: The turn the call belongs to, when it has one.
        requested_model: The model asked for.
    """

    type: ClassVar[str] = "model_call_delivery"
    run_id: str
    logical_call_id: str
    delivery_attempt_id: str
    kind: str
    requested_model: str
    turn_index: int | None = None


@dataclass(frozen=True)
class ModelCallReceiptEvent:
    """How one delivery attempt ended.

    Attributes:
        disposition: `accepted`, `rejected`, `cancelled`, `failed` or
            `not_dispatched`. A cancelled or failed attempt has no `usage`.
        actual_model: The model that served the call, when it was dispatched.
        usage: The call's token usage, `None` when unknown.
        cost_usd: The call's cost, `None` when the model is not priced.
        duration_ms: How long the attempt took.
    """

    type: ClassVar[str] = "model_call_receipt"
    run_id: str
    logical_call_id: str
    delivery_attempt_id: str
    kind: str
    disposition: str
    duration_ms: int
    actual_model: str | None = None
    usage: JsonObject | None = None
    cost_usd: float | None = None


@dataclass(frozen=True)
class ModelCallUsageSettlementEvent:
    """A settled receipt's usage was charged to a budget owner."""

    type: ClassVar[str] = "model_call_usage_settlement"
    run_id: str
    logical_call_id: str
    usage: JsonObject
    budget_owner_id: str | None = None
    cost_usd: float | None = None


@dataclass(frozen=True)
class UnknownEvent:
    """A kind this client has no class for; `raw` is the payload without `type`."""

    type: str
    raw: JsonObject


Event = (
    InitEvent
    | ScheduleFireEvent
    | UserEvent
    | AssistantEvent
    | PartialAssistantEvent
    | UsageEvent
    | ToolCallStartEvent
    | ToolCallEndEvent
    | ToolRetryEvent
    | ToolInterruptedEvent
    | ToolProgressEvent
    | PermissionRequestEvent
    | InputRequestEvent
    | SuspendedEvent
    | ContextBuildEvent
    | CompactionEvent
    | LoopGuardEvent
    | BudgetEvent
    | ProviderRetryEvent
    | ModelFallbackEvent
    | PromptCacheAdvisoryEvent
    | VerificationEvent
    | PolicyEvent
    | SkillsLoadedEvent
    | SkillInvokedEvent
    | SkillCompletedEvent
    | WorkflowEvent
    | SubagentEvent
    | ChildRunStatusEvent
    | HookEvent
    | ErrorEvent
    | ResultEvent
    | ModelCallDeliveryEvent
    | ModelCallReceiptEvent
    | ModelCallUsageSettlementEvent
    | SteerReceivedEvent
    | UnknownEvent
)

_KINDS: dict[str, type[Any]] = {
    cls.type: cls
    for cls in (
        InitEvent,
        ScheduleFireEvent,
        UserEvent,
        AssistantEvent,
        PartialAssistantEvent,
        UsageEvent,
        ToolCallStartEvent,
        ToolCallEndEvent,
        ToolRetryEvent,
        ToolInterruptedEvent,
        ToolProgressEvent,
        PermissionRequestEvent,
        InputRequestEvent,
        SuspendedEvent,
        ContextBuildEvent,
        CompactionEvent,
        LoopGuardEvent,
        BudgetEvent,
        ProviderRetryEvent,
        ModelFallbackEvent,
        PromptCacheAdvisoryEvent,
        VerificationEvent,
        PolicyEvent,
        SkillsLoadedEvent,
        SkillInvokedEvent,
        SkillCompletedEvent,
        WorkflowEvent,
        SubagentEvent,
        ChildRunStatusEvent,
        HookEvent,
        ErrorEvent,
        ResultEvent,
        ModelCallDeliveryEvent,
        ModelCallReceiptEvent,
        ModelCallUsageSettlementEvent,
        SteerReceivedEvent,
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
    """Decode one envelope `{"v", "seq", "run_id", "ts_ms", "event", "ignorable"?}`.

    Raises:
        LinchError: The envelope's `v` is not `WIRE_VERSION`: the server
            speaks a wire this client cannot read.
    """
    version = raw.get("v")
    if version != WIRE_VERSION:
        raise LinchError(
            f"envelope wire version {version!r} is not supported: this client reads "
            f"version {WIRE_VERSION}; use a linch-rust client that matches the server"
        )
    return Envelope(
        seq=int(raw["seq"]),
        run_id=str(raw["run_id"]),
        ts_ms=int(raw["ts_ms"]),
        event=decode_event(raw["event"]),
    )
