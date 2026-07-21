"""Strict v1alpha2 blueprint models.

The catalog is deliberately hand-authored. These models are a design contract,
not a reflection of ``Agent.__init__`` or another mutable runtime surface.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints

API_VERSION = "studio.linch.dev/v1alpha2"
LEGACY_API_VERSION = "studio.linch.dev/v1alpha1"
KIND = "LinchProject"
ID_PATTERN = r"^[a-z][a-z0-9_]*$"
ENV_PATTERN = r"^[A-Z][A-Z0-9_]*$"

Identifier = Annotated[
    str,
    StringConstraints(pattern=ID_PATTERN, min_length=1, max_length=64, strict=True),
]
EnvironmentName = Annotated[
    str,
    StringConstraints(pattern=ENV_PATTERN, min_length=1, max_length=128, strict=True),
]
NonEmptyString = Annotated[str, StringConstraints(min_length=1, max_length=262_144, strict=True)]
ModelName = Annotated[
    str,
    StringConstraints(min_length=1, max_length=256, strict=True),
]


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in tail)


class StudioModel(BaseModel):
    """Base for every persisted Studio object."""

    model_config = ConfigDict(
        alias_generator=_to_camel,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        strict=True,
        allow_inf_nan=False,
    )


class ProjectMetadata(StudioModel):
    name: Identifier
    title: NonEmptyString
    description: str = Field(default="", max_length=262_144)


class TargetSpec(StudioModel):
    python: str = Field(default=">=3.10", pattern=r"^>=3\.(10|11|12|13)(?:,.+)?$")
    linch: str = Field(default=">=1.1,<2", pattern=r"^>=1\.1(?:\.0)?,<2(?:\.0)?$")


ProviderKind = Literal[
    "openai_responses",
    "openai_chat",
    "anthropic",
    "gemini",
    "llama_cpp",
    "vllm",
    "sglang",
    "custom",
]
ApiVersion = Literal["studio.linch.dev/v1alpha2"]
BlueprintKind = Literal["LinchProject"]


class ThinkingSpec(StudioModel):
    enabled: bool = False
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    budget_tokens: int | None = Field(default=None, ge=1, le=1_000_000)


class ProviderSpec(StudioModel):
    kind: ProviderKind | None = None
    model: ModelName | None = None
    api_key_env: EnvironmentName | None = None
    base_url_env: EnvironmentName | None = None
    project_env: EnvironmentName | None = None
    location: str | None = Field(default=None, min_length=1, max_length=128)
    context_window: int | None = Field(default=None, ge=1024, le=10_000_000)
    streaming: bool = True
    thinking: ThinkingSpec = Field(default_factory=ThinkingSpec)
    fallback_models: list[ModelName] = Field(default_factory=list, max_length=16)


class BudgetSpec(StudioModel):
    max_tokens: int | None = Field(default=None, ge=1, le=1_000_000_000)
    max_cost_usd: float | None = Field(default=None, gt=0, le=1_000_000)
    warn_ratio: float = Field(default=0.9, gt=0, le=1)


class TextContainsVerifierSpec(StudioModel):
    kind: Literal["text_contains"] = "text_contains"
    id: Identifier
    text: NonEmptyString
    feedback: str | None = Field(default=None, min_length=1, max_length=16_384)


class JsonSchemaVerifierSpec(StudioModel):
    kind: Literal["json_schema"] = "json_schema"
    id: Identifier
    schema_definition: dict[str, JsonValue] = Field(alias="schema")
    feedback: str | None = Field(default=None, min_length=1, max_length=16_384)


class CustomTodoVerifierSpec(StudioModel):
    kind: Literal["custom_todo"] = "custom_todo"
    id: Identifier
    description: NonEmptyString


VerifierSpec = Annotated[
    TextContainsVerifierSpec | JsonSchemaVerifierSpec | CustomTodoVerifierSpec,
    Field(discriminator="kind"),
]


class CompletionSpec(StudioModel):
    mode: Literal["agent_judged", "verifier_gated"] = "agent_judged"
    max_retries: int = Field(default=2, ge=0, le=100)
    verifiers: list[VerifierSpec] = Field(default_factory=list, max_length=128)


class RuntimeAgentSpec(StudioModel):
    preset: Literal["standard_agent", "deep_agent", "coordinator"] = "standard_agent"
    instructions: str = Field(default="", max_length=262_144)
    tools: list[Identifier] | None = Field(default=None, max_length=128)
    max_turns: int | None = Field(default=None, ge=1, le=10_000)
    budget: BudgetSpec = Field(default_factory=BudgetSpec)
    completion: CompletionSpec = Field(default_factory=CompletionSpec)


class RuntimeSpec(StudioModel):
    provider: ProviderSpec = Field(default_factory=ProviderSpec)
    agent: RuntimeAgentSpec = Field(default_factory=RuntimeAgentSpec)


class ResourceSpec(StudioModel):
    resource: NonEmptyString
    mode: Literal["read", "write"] = "read"


class ToolSpecBase(StudioModel):
    id: Identifier
    display_name: NonEmptyString
    description: NonEmptyString
    scope: Literal["read", "write", "exec"] = "read"
    parallel: bool = True
    retryable: bool = False
    timeout_ms: int | None = Field(default=None, ge=1, le=3_600_000)
    input_schema: dict[str, JsonValue] = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }
    )
    resources: list[ResourceSpec] = Field(default_factory=list, max_length=128)


class FunctionToolSpec(ToolSpecBase):
    kind: Literal["function"] = "function"


class ClassToolSpec(ToolSpecBase):
    kind: Literal["class"] = "class"


class DatabaseToolSpec(ToolSpecBase):
    kind: Literal["database"] = "database"
    operation: Literal["read", "write"] = "read"
    idempotency_argument: Identifier | None = None


ToolSpec = Annotated[
    FunctionToolSpec | ClassToolSpec | DatabaseToolSpec,
    Field(discriminator="kind"),
]


class SubagentSpec(StudioModel):
    id: Identifier
    display_name: NonEmptyString
    description: str = Field(default="", max_length=16_384)
    instructions: NonEmptyString
    tools: list[Identifier] = Field(default_factory=list, max_length=128)


class SkillSpec(StudioModel):
    id: Identifier
    display_name: NonEmptyString
    description: str = Field(default="", max_length=16_384)
    instructions: NonEmptyString
    allowed_tools: list[Identifier] = Field(default_factory=list, max_length=128)


class AgentCallNodeSpec(StudioModel):
    type: Literal["agent_call"] = "agent_call"
    id: Identifier
    label: NonEmptyString
    prompt: NonEmptyString
    depends_on: list[Identifier] = Field(default_factory=list, max_length=256)
    subagent: Identifier | None = None
    tools: list[Identifier] = Field(default_factory=list, max_length=128)
    phase: str | None = Field(default=None, min_length=1, max_length=128)


class DirectToolNodeSpec(StudioModel):
    """Parsed only so validation can return an explicit unsupported diagnostic."""

    type: Literal["direct_tool"] = "direct_tool"
    id: Identifier
    label: NonEmptyString
    tool: Identifier
    depends_on: list[Identifier] = Field(default_factory=list, max_length=256)


WorkflowNodeSpec = Annotated[
    AgentCallNodeSpec | DirectToolNodeSpec,
    Field(discriminator="type"),
]


class WorkflowSpec(StudioModel):
    kind: Literal["directed"] = "directed"
    id: Identifier
    display_name: NonEmptyString
    description: str = Field(default="", max_length=16_384)
    nodes: list[WorkflowNodeSpec] = Field(default_factory=list, max_length=1_000)
    output: Identifier | None = None
    max_concurrency: int = Field(default=4, ge=1, le=128)


class ManualTriggerSpec(StudioModel):
    kind: Literal["manual"] = "manual"
    id: Identifier
    display_name: NonEmptyString


class CronTriggerSpec(StudioModel):
    kind: Literal["cron"] = "cron"
    id: Identifier
    display_name: NonEmptyString
    cron: NonEmptyString
    timezone: NonEmptyString = "UTC"


class CITriggerSpec(StudioModel):
    kind: Literal["ci"] = "ci"
    id: Identifier
    display_name: NonEmptyString
    provider: Literal["generic", "github_actions"] = "generic"


class WebhookTriggerSpec(StudioModel):
    kind: Literal["webhook"] = "webhook"
    id: Identifier
    display_name: NonEmptyString
    signing_secret_env: EnvironmentName | None = None


TriggerSpec = Annotated[
    ManualTriggerSpec | CronTriggerSpec | CITriggerSpec | WebhookTriggerSpec,
    Field(discriminator="kind"),
]


class RoutineSpecBase(StudioModel):
    id: Identifier
    display_name: NonEmptyString
    triggers: list[Identifier] = Field(default_factory=list, max_length=64)
    max_turns: int | None = Field(default=None, ge=1, le=10_000)
    budget: BudgetSpec = Field(default_factory=BudgetSpec)
    verify: VerifierSpec | None = None
    done_when: VerifierSpec | None = None


class AgentTickRoutineSpec(RoutineSpecBase):
    kind: Literal["agent_tick"] = "agent_tick"
    charter: NonEmptyString
    prompt: NonEmptyString
    target: Literal["runtime_agent"] = "runtime_agent"


class WorkflowRunRoutineSpec(RoutineSpecBase):
    kind: Literal["workflow_run"] = "workflow_run"
    target: Identifier


RoutineSpec = Annotated[
    AgentTickRoutineSpec | WorkflowRunRoutineSpec,
    Field(discriminator="kind"),
]


class PromptSectionSpec(StudioModel):
    id: Identifier
    text: NonEmptyString
    placement: Literal["before_defaults", "after_defaults", "after_env"] = "before_defaults"
    cacheable: bool = True


class PromptCapabilitySpec(StudioModel):
    mode: Literal["append", "replace_defaults"] = "append"
    text: str = Field(default="", max_length=262_144)
    sections: list[PromptSectionSpec] = Field(default_factory=list, max_length=128)
    custom_dynamic_policy: bool = False


class RetrySpec(StudioModel):
    max_attempts: int = Field(default=5, ge=1, le=100)
    base_delay_ms: int = Field(default=1_000, ge=0, le=3_600_000)
    max_delay_ms: int = Field(default=30_000, ge=0, le=3_600_000)
    jitter: float = Field(default=0.2, ge=0, le=1)


class LoopGuardSpec(StudioModel):
    enabled: bool = True
    max_identical_tool_calls: int = Field(default=3, ge=0, le=10_000)
    max_consecutive_failures: int = Field(default=3, ge=0, le=10_000)
    force_final_answer: bool = False


class TruncationRecoverySpec(StudioModel):
    enabled: bool = False
    max_attempts: int = Field(default=1, ge=1, le=100)
    feedback: str | None = Field(default=None, min_length=1, max_length=16_384)


class ReliabilityCapabilitySpec(StudioModel):
    provider_retry: RetrySpec = Field(default_factory=RetrySpec)
    tool_timeout_ms: int | None = Field(default=None, ge=1, le=3_600_000)
    tool_retry: RetrySpec | None = None
    max_output_tokens: int | None = Field(default=None, ge=1, le=10_000_000)
    max_tool_concurrency: int | None = Field(default=None, ge=1, le=128)
    loop_guard: LoopGuardSpec = Field(default_factory=LoopGuardSpec)
    truncation_recovery: TruncationRecoverySpec = Field(default_factory=TruncationRecoverySpec)
    custom_token_estimator: bool = False
    custom_recovery_policy: bool = False


class CompactionLadderSpec(StudioModel):
    enabled: bool = False
    micro: bool = True
    keep_recent_turns: int = Field(default=10, ge=1, le=10_000)
    max_forced_compactions: int = Field(default=3, ge=0, le=100)
    reset_read_tracker: bool = True


class CompactionCapabilitySpec(StudioModel):
    strategy: Literal["none", "default_coding", "general", "detailed", "custom"] = "none"
    keep_recent_turns: int = Field(default=10, ge=1, le=10_000)
    max_output_tokens: int = Field(default=8_192, ge=1, le=10_000_000)
    ladder: CompactionLadderSpec = Field(default_factory=CompactionLadderSpec)


class StructuredOutputCapabilitySpec(StudioModel):
    enabled: bool = False
    name: Identifier = "result"
    schema_definition: dict[str, JsonValue] = Field(
        default_factory=lambda: {"type": "object", "additionalProperties": True},
        alias="schema",
    )
    strict: bool = True
    final_tool_name: Identifier | None = None
    repair_retries: int = Field(default=0, ge=0, le=10)


class ContextCapabilitySpec(StudioModel):
    memory_recall: bool = False
    max_tokens: int | None = Field(default=None, ge=1, le=10_000_000)
    max_items: int = Field(default=5, ge=1, le=1_000)
    custom_builder: bool = False
    dynamic_tool_selector: bool = False


class MemoryCapabilitySpec(StudioModel):
    backend: Literal[
        "none",
        "in_memory",
        "sqlite",
        "postgres_keyword",
        "tiered",
        "faiss",
        "pgvector",
        "qdrant",
        "custom",
    ] = "none"
    namespace: str | None = Field(default=None, min_length=1, max_length=128)
    dsn_env: EnvironmentName | None = None
    search_tool: bool = False
    upsert_tool: bool = False
    extraction_hook: bool = False


class FilesystemCapabilitySpec(StudioModel):
    backend: Literal["none", "state", "disk", "sqlite", "composite", "external"] = "none"
    root: str = Field(default=".linch/files", min_length=1, max_length=4_096)
    offload_enabled: bool = False
    offload_threshold_tokens: int | None = Field(default=None, ge=1, le=10_000_000)
    offload_threshold_fraction: float = Field(default=0.1, gt=0, le=1)
    preview_lines: int = Field(default=10, ge=1, le=10_000)


class RedactionRuleSpec(StudioModel):
    pattern: NonEmptyString
    replacement: str = Field(default="[REDACTED]", max_length=16_384)


class HooksCapabilitySpec(StudioModel):
    tool_cache: bool = False
    read_before_write: bool = True
    stop_predicate_skeleton: bool = False
    custom_middleware: bool = False
    redaction_rules: list[RedactionRuleSpec] = Field(default_factory=list, max_length=128)


class ObservationCapabilitySpec(StudioModel):
    logging: bool = True
    run_report: bool = True
    otel: bool = False
    vendor_exporter: str | None = Field(default=None, min_length=1, max_length=128)


class PersistenceCapabilitySpec(StudioModel):
    session_store: Literal["in_memory", "sqlite", "external"] = "in_memory"
    run_store: Literal["in_memory", "sqlite", "external"] = "in_memory"
    durable_resume: bool = False


class ToolPermissionRuleSpec(StudioModel):
    kind: Literal["tool"] = "tool"
    tool: NonEmptyString
    decision: Literal["allow", "deny", "ask", "passthrough"] = "ask"
    argument: str | None = Field(default=None, min_length=1, max_length=1_024)


class PathPermissionRuleSpec(StudioModel):
    kind: Literal["path"] = "path"
    paths: list[NonEmptyString] = Field(min_length=1, max_length=128)
    decision: Literal["allow", "deny", "ask", "passthrough"] = "ask"
    tools: list[NonEmptyString] = Field(default_factory=list, max_length=128)


class BashPermissionRuleSpec(StudioModel):
    kind: Literal["bash"] = "bash"
    patterns: list[NonEmptyString] = Field(min_length=1, max_length=128)
    decision: Literal["allow", "deny", "ask", "passthrough"] = "ask"


PermissionRuleSpec = Annotated[
    ToolPermissionRuleSpec | PathPermissionRuleSpec | BashPermissionRuleSpec,
    Field(discriminator="kind"),
]


class PermissionsCapabilitySpec(StudioModel):
    mode: Literal["interactive", "accept_edits", "trusted"] = "interactive"
    rules: list[PermissionRuleSpec] = Field(default_factory=list, max_length=1_000)
    hitl_callback_skeleton: bool = False
    organization_policy_skeleton: bool = False


class McpStdioSpec(StudioModel):
    kind: Literal["stdio"] = "stdio"
    id: Identifier
    command: NonEmptyString
    args: list[str] = Field(default_factory=list, max_length=128)
    env: dict[EnvironmentName, EnvironmentName] = Field(default_factory=dict)


class McpHttpSpec(StudioModel):
    kind: Literal["http"] = "http"
    id: Identifier
    url: NonEmptyString
    token_env: EnvironmentName | None = None


McpServerSpec = Annotated[McpStdioSpec | McpHttpSpec, Field(discriminator="kind")]


class ExtensionsCapabilitySpec(StudioModel):
    mcp_servers: list[McpServerSpec] = Field(default_factory=list, max_length=128)
    live_mcp_discovery: bool = False
    mailbox_adapter: Literal["none", "in_memory", "sqlite", "external"] = "none"
    schedule_store_adapter: Literal["none", "in_memory", "sqlite", "external"] = "none"
    isolation_adapter: Literal["none", "tempdir", "external"] = "none"


class EvalCaseSpec(StudioModel):
    id: Identifier
    prompt: NonEmptyString
    expected_contains: str | None = Field(default=None, min_length=1, max_length=16_384)


class EvalsCapabilitySpec(StudioModel):
    enabled: bool = False
    cases: list[EvalCaseSpec] = Field(default_factory=list, max_length=1_000)
    domain_scorer_skeleton: bool = False


class ExternalDatabaseCapabilitySpec(StudioModel):
    enabled: bool = False
    dsn_env: EnvironmentName | None = None
    adapter_templates: list[
        Literal[
            "memory_store",
            "file_backend",
            "schedule_store",
            "mailbox",
            "session_store",
            "run_store",
        ]
    ] = Field(default_factory=list, max_length=6)
    vector_guidance: list[Literal["faiss", "pgvector", "qdrant"]] = Field(
        default_factory=list, max_length=3
    )


class CapabilitySelection(StudioModel):
    prompt: PromptCapabilitySpec = Field(default_factory=PromptCapabilitySpec)
    reliability: ReliabilityCapabilitySpec = Field(default_factory=ReliabilityCapabilitySpec)
    compaction: CompactionCapabilitySpec = Field(default_factory=CompactionCapabilitySpec)
    structured_output: StructuredOutputCapabilitySpec = Field(
        default_factory=StructuredOutputCapabilitySpec
    )
    context: ContextCapabilitySpec = Field(default_factory=ContextCapabilitySpec)
    memory: MemoryCapabilitySpec = Field(default_factory=MemoryCapabilitySpec)
    filesystem: FilesystemCapabilitySpec = Field(default_factory=FilesystemCapabilitySpec)
    hooks: HooksCapabilitySpec = Field(default_factory=HooksCapabilitySpec)
    observation: ObservationCapabilitySpec = Field(default_factory=ObservationCapabilitySpec)
    persistence: PersistenceCapabilitySpec = Field(default_factory=PersistenceCapabilitySpec)
    permissions: PermissionsCapabilitySpec = Field(default_factory=PermissionsCapabilitySpec)
    extensions: ExtensionsCapabilitySpec = Field(default_factory=ExtensionsCapabilitySpec)
    evals: EvalsCapabilitySpec = Field(default_factory=EvalsCapabilitySpec)
    external_database: ExternalDatabaseCapabilitySpec = Field(
        default_factory=ExternalDatabaseCapabilitySpec
    )


class ProjectSpec(StudioModel):
    package: Identifier
    target: TargetSpec = Field(default_factory=TargetSpec)
    runtime: RuntimeSpec = Field(default_factory=RuntimeSpec)
    tools: list[ToolSpec] = Field(default_factory=list, max_length=1_000)
    subagents: list[SubagentSpec] = Field(default_factory=list, max_length=1_000)
    skills: list[SkillSpec] = Field(default_factory=list, max_length=1_000)
    workflows: list[WorkflowSpec] = Field(default_factory=list, max_length=1_000)
    routines: list[RoutineSpec] = Field(default_factory=list, max_length=1_000)
    triggers: list[TriggerSpec] = Field(default_factory=list, max_length=1_000)
    capabilities: CapabilitySelection = Field(default_factory=CapabilitySelection)


class Blueprint(StudioModel):
    api_version: ApiVersion
    kind: BlueprintKind
    metadata: ProjectMetadata
    spec: ProjectSpec


__all__ = [
    "API_VERSION",
    "ENV_PATTERN",
    "ID_PATTERN",
    "KIND",
    "LEGACY_API_VERSION",
    "AgentTickRoutineSpec",
    "AgentCallNodeSpec",
    "ApiVersion",
    "Blueprint",
    "BlueprintKind",
    "CapabilitySelection",
    "CompletionSpec",
    "CustomTodoVerifierSpec",
    "DatabaseToolSpec",
    "EnvironmentName",
    "Identifier",
    "JsonSchemaVerifierSpec",
    "ModelName",
    "ProjectMetadata",
    "ProjectSpec",
    "ProviderSpec",
    "RoutineSpec",
    "RuntimeAgentSpec",
    "RuntimeSpec",
    "StudioModel",
    "TextContainsVerifierSpec",
    "ToolSpec",
    "VerifierSpec",
    "WorkflowSpec",
    "WorkflowRunRoutineSpec",
]
