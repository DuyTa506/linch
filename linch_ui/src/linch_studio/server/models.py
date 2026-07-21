"""Strict request and response models for the local Studio API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from linch_studio.authoring import (
    MAX_INSTRUCTION_CHARS,
    MAX_MESSAGE_CHARS,
    MAX_TOOL_DETAIL_CHARS,
    MAX_TOOL_SUMMARY_CHARS,
    MAX_TRANSCRIPT_CHARS,
    MAX_TRANSCRIPT_MESSAGES,
    SemanticDiffEntry,
)
from linch_studio.catalog import StatusId as CatalogStatusId
from linch_studio.spec import Blueprint, Diagnostic


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in tail)


class ApiModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        extra="forbid",
        populate_by_name=True,
        strict=True,
    )


class ProjectCreateRequest(ApiModel):
    project_id: str = Field(alias="id", min_length=1, max_length=64)
    title: str | None = Field(default=None, min_length=1, max_length=512)
    template: Literal[
        "agent",
        "goal_verified",
        "directed_workflow",
        "coordinator",
        "routine",
    ] = "agent"


class SaveBlueprintRequest(ApiModel):
    yaml: str = Field(min_length=1, max_length=1_048_576)
    base_digest: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")


class ValidateBlueprintRequest(ApiModel):
    yaml: str = Field(min_length=1, max_length=1_048_576)


class LayoutNode(ApiModel):
    id: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_]*$")
    x: float = Field(ge=-1_000_000, le=1_000_000, allow_inf_nan=False)
    y: float = Field(ge=-1_000_000, le=1_000_000, allow_inf_nan=False)
    width: float | None = Field(default=None, ge=0, le=100_000, allow_inf_nan=False)
    height: float | None = Field(default=None, ge=0, le=100_000, allow_inf_nan=False)


class LayoutViewport(ApiModel):
    x: float = Field(default=0, ge=-1_000_000, le=1_000_000, allow_inf_nan=False)
    y: float = Field(default=0, ge=-1_000_000, le=1_000_000, allow_inf_nan=False)
    zoom: float = Field(default=1, gt=0, le=100, allow_inf_nan=False)


class LayoutDocument(ApiModel):
    nodes: list[LayoutNode] = Field(default_factory=list, max_length=10_000)
    viewport: LayoutViewport = Field(default_factory=LayoutViewport)


class ProjectSummary(ApiModel):
    project_id: str = Field(alias="id")
    title: str
    digest: str
    export_ready: bool
    updated_at: str
    model: str | None = None


class ProjectListResponse(ApiModel):
    projects: list[ProjectSummary]


class MigrationWarningResponse(ApiModel):
    code: str
    path: str
    message: str
    remediation: str
    requires_confirmation: bool = True


class ProjectDocument(ApiModel):
    project_id: str = Field(alias="id")
    yaml: str
    blueprint: Blueprint
    digest: str
    diagnostics: list[Diagnostic]
    export_ready: bool
    layout: LayoutDocument
    migrated_from: str | None = None
    migration_warnings: list[MigrationWarningResponse] = Field(default_factory=list)


class ValidationResponse(ApiModel):
    structurally_valid: bool
    export_ready: bool
    digest: str | None = None
    diagnostics: list[Diagnostic]


class LayoutResponse(ApiModel):
    project_id: str = Field(alias="id")
    layout: LayoutDocument


class GeneratedFilePreview(ApiModel):
    path: str
    sha256: str
    size: int
    capability_id: str
    content: str


class ExportPreviewResponse(ApiModel):
    blueprint_digest: str
    files: list[GeneratedFilePreview]


class DirectoryExportRequest(ApiModel):
    target: str = Field(min_length=1, max_length=4_096)


class DirectoryExportResponse(ApiModel):
    target: str
    blueprint_digest: str


class ProposalRequest(ApiModel):
    instruction: str = Field(min_length=1, max_length=MAX_INSTRUCTION_CHARS)


class ProposalResponse(ApiModel):
    id: str
    project_id: str
    base_digest: str
    candidate_digest: str
    candidate: Blueprint
    semantic_diff: list[SemanticDiffEntry]
    diagnostics: list[Diagnostic]
    export_ready: bool
    created_at: str
    summary: str | None = None


class ProposalListResponse(ApiModel):
    proposals: list[ProposalResponse]


class TurnMessage(ApiModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


class TurnRequest(ApiModel):
    messages: list[TurnMessage] = Field(min_length=1, max_length=MAX_TRANSCRIPT_MESSAGES)
    stage: Literal["chat", "build"] = "chat"

    @model_validator(mode="after")
    def _bounded_conversation(self) -> TurnRequest:
        if sum(len(item.content) for item in self.messages) > MAX_TRANSCRIPT_CHARS:
            raise ValueError("transcript exceeds the total size limit")
        if self.messages[-1].role != "user":
            raise ValueError("transcript must end with a user message")
        return self


class TurnQuestion(ApiModel):
    question: str
    options: list[str]


class TurnToolCall(ApiModel):
    """One read-only knowledge-tool call, for the chat UI's compact tool-call row."""

    tool_use_id: str
    tool_name: str
    summary: str = Field(max_length=MAX_TOOL_SUMMARY_CHARS)
    result_summary: str | None = Field(default=None, max_length=MAX_TOOL_SUMMARY_CHARS)
    detail: str | None = Field(default=None, max_length=MAX_TOOL_DETAIL_CHARS)
    is_error: bool = False
    duration_ms: int = Field(default=0, ge=0)


class TurnResponse(ApiModel):
    kind: Literal["questions", "plan", "proposal"]
    questions: list[TurnQuestion] = Field(default_factory=list)
    plan: str | None = None
    plan_note: str | None = None
    proposal: ProposalResponse | None = None
    thinking: str | None = None
    tool_calls: list[TurnToolCall] = Field(default_factory=list)


# ---- Global support -------------------------------------------------------
# Support has its own wire shape so documentation and implementation requests
# never inherit the authoring-only questions → plan → Blueprint contract.


class SupportTurnRequest(ApiModel):
    messages: list[TurnMessage] = Field(min_length=1, max_length=MAX_TRANSCRIPT_MESSAGES)
    requested_mode: Literal["auto", "documentation", "implementation", "pipeline"] = "auto"
    pipeline_confirmed: bool = False
    project_id: str | None = Field(default=None, min_length=1, max_length=64)
    stage: Literal["chat", "build"] = "chat"
    approved_plan_digest: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$"
    )

    @model_validator(mode="after")
    def _bounded_conversation(self) -> SupportTurnRequest:
        if sum(len(item.content) for item in self.messages) > MAX_TRANSCRIPT_CHARS:
            raise ValueError("transcript exceeds the total size limit")
        if self.messages[-1].role != "user":
            raise ValueError("transcript must end with a user message")
        if self.stage == "build" and not self.pipeline_confirmed:
            raise ValueError("only a confirmed pipeline can enter the build stage")
        return self


class SupportEvidence(ApiModel):
    anchor: str
    claim: str
    excerpt: str | None = None


class SupportRecipeFile(ApiModel):
    path: str
    language: Literal["python", "toml", "yaml", "text", "shell"]
    content: str
    provenance: Literal["copied", "composed", "skeleton"]
    evidence: list[str] = Field(default_factory=list)
    explanation: str | None = None


class SupportRecipeCommand(ApiModel):
    command: str
    purpose: str


class SupportRecipeTodo(ApiModel):
    description: str
    blocking: bool = True
    owner: Literal["developer", "host", "security"] = "developer"


class SupportHandoff(ApiModel):
    start_here: list[str]
    environment: list[str] = Field(default_factory=list)
    commands: list[SupportRecipeCommand] = Field(default_factory=list)
    todos: list[SupportRecipeTodo] = Field(default_factory=list)


class SupportImplementationIntent(ApiModel):
    summary: str
    capabilities: list[str] = Field(default_factory=list)
    schedule: str | None = None
    workflow_shape: str | None = None
    constraints: list[str] = Field(default_factory=list)


class SupportRecipe(ApiModel):
    title: str
    overview: str
    intent: SupportImplementationIntent
    files: list[SupportRecipeFile]
    tests: list[SupportRecipeFile] = Field(default_factory=list)
    handoff: SupportHandoff


class SupportPipelineIntent(ApiModel):
    summary: str
    proposed_components: list[str] = Field(default_factory=list)
    requires_project: bool = True


class SupportTurnResponse(ApiModel):
    """One global support turn; exactly one matching artifact is populated."""

    kind: Literal["answer", "recipe", "mode_confirmation", "questions", "plan", "proposal"]
    mode: Literal["documentation", "implementation", "pipeline"]
    answer: str | None = None
    recipe: SupportRecipe | None = None
    pipeline_intent: SupportPipelineIntent | None = None
    evidence: list[SupportEvidence] = Field(default_factory=list)
    coverage: Literal["documented", "partial", "not_found"] = "partial"
    follow_up: str | None = None
    questions: list[TurnQuestion] = Field(default_factory=list)
    plan: str | None = None
    plan_digest: str | None = None
    proposal: ProposalResponse | None = None
    tool_calls: list[TurnToolCall] = Field(default_factory=list)


class CatalogVersionResponse(ApiModel):
    api_version: str
    revision: int
    target_linch: str
    source: Literal["hand_authored"]


class CapabilityStatusResponse(ApiModel):
    id: CatalogStatusId
    badge: str
    description: str
    order: int
    export_allowed: bool


class CapabilityAreaResponse(ApiModel):
    id: str
    title: str
    description: str
    order: int


class CapabilityRecordResponse(ApiModel):
    id: str
    title: str
    summary: str
    area: str
    status: CatalogStatusId
    badge: str
    export_allowed: bool
    execution_model: bool


class RelationMatrixVersionResponse(ApiModel):
    api_version: str
    revision: int
    blueprint_api_version: str


class EditableRelationResponse(ApiModel):
    id: str
    source_kind: str
    target_kind: str
    relation: str
    decision: Literal["allow", "deny"]
    field: str | None = None
    constraints: list[str]
    reason: str
    order: int


class EditableRelationMatrixResponse(ApiModel):
    version: RelationMatrixVersionResponse
    relations: list[EditableRelationResponse]


class CapabilityCatalogResponse(ApiModel):
    """Wire projection of the hand-authored capability catalog.

    Mirrors ``CapabilityCatalog.to_dict``; ``test_catalog_response_still_matches_
    catalog_document`` fails if the two drift apart.
    """

    catalog_version: CatalogVersionResponse
    statuses: list[CapabilityStatusResponse]
    areas: list[CapabilityAreaResponse]
    capabilities: list[CapabilityRecordResponse]
    relation_matrix: EditableRelationMatrixResponse


class ErrorBody(ApiModel):
    code: str
    message: str
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    current_digest: str | None = None


class ErrorEnvelope(ApiModel):
    error: ErrorBody


class ServiceInfo(ApiModel):
    name: Literal["linch-studio"] = "linch-studio"
    api_version: Literal["v1"] = "v1"
    authoring_available: bool = False
    support_available: bool = False


__all__ = [
    "ApiModel",
    "DirectoryExportRequest",
    "DirectoryExportResponse",
    "ErrorBody",
    "ErrorEnvelope",
    "EditableRelationMatrixResponse",
    "EditableRelationResponse",
    "ExportPreviewResponse",
    "GeneratedFilePreview",
    "LayoutDocument",
    "LayoutNode",
    "LayoutResponse",
    "LayoutViewport",
    "MigrationWarningResponse",
    "ProjectCreateRequest",
    "ProjectDocument",
    "ProjectListResponse",
    "ProjectSummary",
    "RelationMatrixVersionResponse",
    "ProposalListResponse",
    "ProposalRequest",
    "ProposalResponse",
    "SaveBlueprintRequest",
    "ServiceInfo",
    "SupportEvidence",
    "SupportHandoff",
    "SupportImplementationIntent",
    "SupportPipelineIntent",
    "SupportRecipe",
    "SupportRecipeCommand",
    "SupportRecipeFile",
    "SupportRecipeTodo",
    "SupportTurnRequest",
    "SupportTurnResponse",
    "TurnMessage",
    "TurnQuestion",
    "TurnRequest",
    "TurnResponse",
    "ValidateBlueprintRequest",
    "ValidationResponse",
]
