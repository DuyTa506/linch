"""Typed, documentation-grounded artifacts returned by the Studio support agent.

Support is intentionally separate from blueprint authoring.  A developer can ask
for an explanation or an implementation recipe without creating or mutating a
Studio project; only the explicit pipeline bridge enters the authoring lifecycle.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from linch_studio.authoring.models import (
    MAX_MESSAGE_CHARS,
    MAX_TRANSCRIPT_CHARS,
    MAX_TRANSCRIPT_MESSAGES,
    ProposalTelemetry,
)

SupportMode = Literal["auto", "documentation", "implementation", "pipeline"]
ResolvedSupportMode = Literal["documentation", "implementation", "pipeline"]
SupportKind = Literal["answer", "recipe", "mode_confirmation", "questions", "plan", "proposal"]
EvidenceCoverage = Literal["documented", "partial", "not_found"]
CodeProvenance = Literal["copied", "composed", "skeleton"]

MAX_EVIDENCE = 12
MAX_FILES = 24
MAX_CODE_CHARS = 48_000
MAX_SUMMARY_CHARS = 8_000
MAX_TODO_CHARS = 2_000


class SupportModel(BaseModel):
    """Strict, frozen transport model shared by the service and API boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SupportMessage(SupportModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


class Evidence(SupportModel):
    """A compact provenance pointer, never provider reasoning or a raw document."""

    anchor: str = Field(min_length=3, max_length=512)
    claim: str = Field(min_length=1, max_length=600)
    excerpt: str | None = Field(default=None, max_length=800)


class ImplementationIntent(SupportModel):
    """Normalized implementation goal carried into an optional pipeline bridge."""

    summary: str = Field(min_length=1, max_length=1_500)
    capabilities: list[Annotated[str, Field(min_length=1, max_length=120)]] = Field(
        default_factory=list, max_length=16
    )
    schedule: str | None = Field(default=None, max_length=500)
    workflow_shape: str | None = Field(default=None, max_length=800)
    constraints: list[Annotated[str, Field(min_length=1, max_length=400)]] = Field(
        default_factory=list, max_length=16
    )


class RecipeFile(SupportModel):
    path: str = Field(min_length=1, max_length=300)
    language: Literal["python", "toml", "yaml", "text", "shell"] = "python"
    content: str = Field(min_length=1, max_length=MAX_CODE_CHARS)
    provenance: CodeProvenance = "composed"
    evidence: list[Annotated[str, Field(min_length=3, max_length=512)]] = Field(
        default_factory=list, max_length=MAX_EVIDENCE
    )
    explanation: str | None = Field(default=None, max_length=1_200)

    @field_validator("path")
    @classmethod
    def _safe_relative_path(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        if normalized.startswith("/") or ".." in normalized.split("/") or "//" in normalized:
            raise ValueError("recipe file paths must be safe relative paths")
        return normalized


class RecipeCommand(SupportModel):
    command: str = Field(min_length=1, max_length=1_000)
    purpose: str = Field(min_length=1, max_length=500)


class RecipeTodo(SupportModel):
    description: str = Field(min_length=1, max_length=MAX_TODO_CHARS)
    blocking: bool = True
    owner: Literal["developer", "host", "security"] = "developer"


class DeveloperHandoff(SupportModel):
    """A concise, execution-oriented guide emitted beside recipes and exports."""

    start_here: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(
        min_length=1, max_length=12
    )
    environment: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(
        default_factory=list, max_length=20
    )
    commands: list[RecipeCommand] = Field(default_factory=list, max_length=16)
    todos: list[RecipeTodo] = Field(default_factory=list, max_length=20)


class ImplementationRecipe(SupportModel):
    title: str = Field(min_length=1, max_length=300)
    overview: str = Field(min_length=1, max_length=MAX_SUMMARY_CHARS)
    intent: ImplementationIntent
    files: list[RecipeFile] = Field(min_length=1, max_length=MAX_FILES)
    tests: list[RecipeFile] = Field(default_factory=list, max_length=MAX_FILES)
    handoff: DeveloperHandoff

    @model_validator(mode="after")
    def _unique_paths(self) -> ImplementationRecipe:
        paths = [item.path for item in (*self.files, *self.tests)]
        if len(paths) != len(set(paths)):
            raise ValueError("recipe file paths must be unique")
        return self


class PipelineIntent(SupportModel):
    summary: str = Field(min_length=1, max_length=1_500)
    proposed_components: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(
        default_factory=list, max_length=16
    )
    requires_project: bool = True


class SupportTurnOutput(SupportModel):
    """Strict model output for documentation and implementation requests only.

    Pipeline confirmation and authoring turns are deterministic server concerns;
    keeping them out of the model schema prevents an answer request from silently
    turning into a project mutation proposal.
    """

    kind: Literal["answer", "recipe"]
    answer: str | None = Field(default=None, min_length=1, max_length=MAX_SUMMARY_CHARS)
    recipe: ImplementationRecipe | None = None
    evidence: list[Evidence] = Field(default_factory=list, max_length=MAX_EVIDENCE)
    coverage: EvidenceCoverage = "partial"
    follow_up: str | None = Field(default=None, max_length=1_000)

    @model_validator(mode="after")
    def _turn_shape(self) -> SupportTurnOutput:
        if self.kind == "answer" and (self.answer is None or self.recipe is not None):
            raise ValueError("an answer turn requires answer only")
        if self.kind == "recipe" and (self.recipe is None or self.answer is not None):
            raise ValueError("a recipe turn requires recipe only")
        return self


class SupportTurn(SupportModel):
    """Public, non-mutating support result.

    ``proposal`` is deliberately represented by ``pipeline_intent`` rather
    than a blueprint.  The server's explicit bridge delegates actual blueprint
    creation to the existing authoring service and its safeguards.
    """

    kind: SupportKind
    mode: ResolvedSupportMode
    answer: str | None = None
    recipe: ImplementationRecipe | None = None
    pipeline_intent: PipelineIntent | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    coverage: EvidenceCoverage = "partial"
    follow_up: str | None = None
    telemetry: ProposalTelemetry | None = None

    @model_validator(mode="after")
    def _public_shape(self) -> SupportTurn:
        if self.kind == "answer" and self.answer is None:
            raise ValueError("answer turn requires an answer")
        if self.kind == "recipe" and self.recipe is None:
            raise ValueError("recipe turn requires a recipe")
        if self.kind == "mode_confirmation" and self.pipeline_intent is None:
            raise ValueError("mode confirmation requires pipeline intent")
        return self


def validate_messages(
    messages: Sequence[SupportMessage],
) -> tuple[SupportMessage, ...]:
    """Validate the client-held, stateless transcript before an LLM is called."""

    bounded = tuple(messages)
    if not bounded:
        raise ValueError("a support conversation requires at least one message")
    if len(bounded) > MAX_TRANSCRIPT_MESSAGES:
        raise ValueError(f"support transcripts are limited to {MAX_TRANSCRIPT_MESSAGES} messages")
    if sum(len(item.content) for item in bounded) > MAX_TRANSCRIPT_CHARS:
        raise ValueError(f"support transcripts are limited to {MAX_TRANSCRIPT_CHARS} characters")
    if bounded[-1].role != "user":
        raise ValueError("support transcripts must end with a user message")
    return bounded


__all__ = [
    "CodeProvenance",
    "DeveloperHandoff",
    "Evidence",
    "EvidenceCoverage",
    "ImplementationIntent",
    "ImplementationRecipe",
    "MAX_CODE_CHARS",
    "MAX_EVIDENCE",
    "PipelineIntent",
    "RecipeCommand",
    "RecipeFile",
    "RecipeTodo",
    "ResolvedSupportMode",
    "SupportKind",
    "SupportMessage",
    "SupportMode",
    "SupportTurn",
    "SupportTurnOutput",
    "validate_messages",
]
