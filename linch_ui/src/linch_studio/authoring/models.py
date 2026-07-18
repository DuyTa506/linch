"""Value-safe records returned by optional AI authoring."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from linch_studio.spec import Blueprint, Diagnostic

TelemetryStatus = Literal[
    "success",
    "questions",
    "plan",
    "malformed",
    "semantic_invalid",
    "manual_only",
    "timeout",
    "budget_exhausted",
    "runtime_error",
]

AuthoringStage = Literal["chat", "build"]

MAX_QUESTIONS = 5
MAX_QUESTION_CHARS = 500
MAX_QUESTION_OPTIONS = 4
MAX_OPTION_CHARS = 200
MAX_NOTE_CHARS = 2_000
MAX_PLAN_CHARS = 8_000
MAX_TRANSCRIPT_MESSAGES = 24
MAX_MESSAGE_CHARS = 32_768
MAX_TRANSCRIPT_CHARS = 131_072
MAX_TOOL_SUMMARY_CHARS = 200
# Comfortably above the largest legitimate single-tool output today (the
# knowledge base's read_section cap is 6_000 chars); a generous but bounded
# ceiling so display never grows unbounded even if a future tool is chattier.
MAX_TOOL_DETAIL_CHARS = 8_000
DiffOperation = Literal["add", "remove", "replace"]
ProposalState = Literal["pending", "accepted", "rejected"]
ToolCallPhase = Literal["start", "end"]


class AuthoringModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SemanticDiffEntry(AuthoringModel):
    """One deterministic, JSON-pointer-addressed semantic change."""

    operation: DiffOperation
    path: str = Field(min_length=1, pattern=r"^/")
    before: JsonValue | None = None
    after: JsonValue | None = None


class ProposalTelemetry(AuthoringModel):
    """Aggregate-only authoring telemetry.

    This intentionally has no free-form metadata or error-message field. Prompt
    text, blueprint bodies, provider reasoning, and secret values therefore
    have nowhere to be recorded.
    """

    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=256)
    duration_ms: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_read_tokens: int = Field(ge=0)
    cache_creation_tokens: int = Field(ge=0)
    status: TelemetryStatus

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_creation_tokens
        )


class Proposal(AuthoringModel):
    """A reviewable full-blueprint proposal that has not modified project state."""

    id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$",
    )
    base_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate: Blueprint
    diagnostics: tuple[Diagnostic, ...] = ()
    diff: tuple[SemanticDiffEntry, ...] = ()
    telemetry: ProposalTelemetry
    summary: str | None = Field(default=None, max_length=MAX_NOTE_CHARS)


class AuthoringMessage(AuthoringModel):
    """One prior conversation turn; the client re-sends the whole transcript."""

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


class AuthoringQuestion(AuthoringModel):
    """One clarifying question with concrete answer options.

    The UI always appends its own free-form option, so the model only supplies
    the concrete choices.
    """

    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    options: list[Annotated[str, Field(min_length=1, max_length=MAX_OPTION_CHARS)]] = Field(
        min_length=2, max_length=MAX_QUESTION_OPTIONS
    )


class AuthoringTurnOutput(AuthoringModel):
    """The model's single reply shape: ask, plan, or build.

    Every field is required so the JSON schema stays a strict-mode-safe root
    object (no top-level union); unused fields must be null. Exactly one of
    ``questions``, ``plan``, and ``blueprint`` may be set — the verifier
    enforces it, and ``blueprint`` is only accepted in the build stage.
    """

    questions: list[AuthoringQuestion] | None = Field(min_length=1, max_length=MAX_QUESTIONS)
    plan: str | None = Field(min_length=1, max_length=MAX_PLAN_CHARS)
    note: str | None = Field(max_length=MAX_NOTE_CHARS)
    blueprint: Blueprint | None
    summary: str | None = Field(max_length=MAX_NOTE_CHARS)


class ToolCallRecord(AuthoringModel):
    """One read-only knowledge-tool call, for the chat UI's compact tool-call row.

    Both tools the authoring agent may call only ever touch the committed docs
    snapshot and the live capability catalog — never project data or secrets —
    so ``detail`` is always safe to display in full behind an expand toggle.
    """

    tool_use_id: str = Field(min_length=1, max_length=128)
    tool_name: str = Field(min_length=1, max_length=128)
    # Pre-call, one line (e.g. "search_docs: fan-out review").
    summary: str = Field(min_length=1, max_length=MAX_TOOL_SUMMARY_CHARS)
    # Post-call, one line (e.g. "3 results"); absent only if the run was
    # interrupted before the call finished.
    result_summary: str | None = Field(default=None, max_length=MAX_TOOL_SUMMARY_CHARS)
    detail: str | None = Field(default=None, max_length=MAX_TOOL_DETAIL_CHARS)
    is_error: bool = False
    duration_ms: int = Field(default=0, ge=0)


@dataclass(slots=True, frozen=True)
class ToolCallUpdate:
    """Live start/end notice for `on_tool_call`; not persisted or transcript-shaped."""

    phase: ToolCallPhase
    tool_use_id: str
    tool_name: str
    summary: str
    detail: str | None = None
    is_error: bool = False
    duration_ms: int = 0


class AuthoringTurn(AuthoringModel):
    """One completed authoring turn as returned to the caller."""

    kind: Literal["questions", "plan", "proposal"]
    questions: tuple[AuthoringQuestion, ...] = ()
    plan: str | None = None
    note: str | None = None
    proposal: Proposal | None = None
    telemetry: ProposalTelemetry
    # Display-only reasoning trace; never re-sent in transcripts, never stored.
    thinking: str | None = None
    # Display-only tool-call trace, oldest first; never re-sent in transcripts.
    tool_calls: tuple[ToolCallRecord, ...] = ()


__all__ = [
    "MAX_MESSAGE_CHARS",
    "MAX_NOTE_CHARS",
    "MAX_OPTION_CHARS",
    "MAX_PLAN_CHARS",
    "MAX_QUESTION_CHARS",
    "MAX_QUESTION_OPTIONS",
    "MAX_QUESTIONS",
    "MAX_TOOL_DETAIL_CHARS",
    "MAX_TOOL_SUMMARY_CHARS",
    "MAX_TRANSCRIPT_CHARS",
    "MAX_TRANSCRIPT_MESSAGES",
    "AuthoringMessage",
    "AuthoringQuestion",
    "AuthoringStage",
    "AuthoringTurn",
    "AuthoringTurnOutput",
    "DiffOperation",
    "Proposal",
    "ProposalState",
    "ProposalTelemetry",
    "SemanticDiffEntry",
    "ToolCallPhase",
    "ToolCallRecord",
    "ToolCallUpdate",
    "TelemetryStatus",
]
