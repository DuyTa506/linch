"""Versioned, hand-authored capability records for Linch Studio.

Nothing in this module imports or introspects Linch runtime objects. Catalog changes
are deliberate product-contract changes and must bump the catalog revision.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence

from .models import (
    CapabilityArea,
    CapabilityBadge,
    CapabilityCatalog,
    CapabilityRecord,
    CapabilityStatus,
    CatalogVersion,
    EditableRelation,
    EditableRelationMatrix,
    RelationConstraint,
    RelationDecision,
    RelationEndpointKind,
    RelationMatrixVersion,
)

CATALOG_VERSION = CatalogVersion(
    api_version="studio.linch.dev/catalog/v1alpha2",
    revision=2,
    target_linch=">=1.1,<2",
)

RUNTIME_READY = CapabilityStatus(
    id="runtime_ready",
    badge="Runtime-ready",
    description="Studio emits complete Linch wiring for this capability.",
    order=0,
    export_allowed=True,
)
SKELETON_TODO = CapabilityStatus(
    id="skeleton_todo",
    badge="Skeleton/TODO",
    description=(
        "Studio emits an explicit implementation seam and visible TODO; runtime behavior "
        "remains blocked until it is implemented."
    ),
    order=1,
    export_allowed=True,
)
UNSUPPORTED = CapabilityStatus(
    id="unsupported",
    badge="Unsupported",
    description="Studio rejects this design and does not emit approximate code.",
    order=2,
    export_allowed=False,
)

STATUSES = (RUNTIME_READY, SKELETON_TODO, UNSUPPORTED)


def _area(identifier: str, title: str, description: str, order: int) -> CapabilityArea:
    return CapabilityArea(identifier, title, description, order)


EXECUTION = _area("execution", "Run shapes", "Supported generated execution shapes.", 0)
PROVIDERS = _area("providers", "Providers", "Provider and model configuration.", 10)
PROMPT = _area("prompt", "Prompt", "System prompt assembly and cache boundaries.", 20)
RELIABILITY = _area("reliability", "Reliability", "Limits, retry, and recovery controls.", 30)
COMPACTION = _area("compaction", "Compaction", "Context compaction strategies.", 40)
STRUCTURED_OUTPUT = _area(
    "structured_output",
    "Structured output",
    "Schema-constrained final result capture.",
    50,
)
COMPLETION = _area(
    "completion",
    "Completion",
    "Agent-judged completion and verifier-gated feedback.",
    55,
)
TOOLS = _area("tools", "Tools", "Generated tool contracts and implementation seams.", 60)
CONTEXT_RAG = _area("context_rag", "Context/RAG", "Per-turn contextual injection.", 70)
MEMORY = _area("memory", "Memory", "Memory stores, recall, and extraction.", 80)
FILESYSTEM = _area("filesystem", "Filesystem", "Virtual files and result offload.", 90)
HOOKS = _area("hooks", "Hooks", "Lifecycle hooks and policy adapters.", 100)
OBSERVATION = _area("observation", "Observation", "Events, reports, and telemetry.", 110)
PERSISTENCE = _area("persistence", "Persistence", "Session and run durability.", 120)
PERMISSIONS = _area("permissions", "Permissions", "Ordered tool access policy.", 130)
EXTENSIONS = _area("extensions", "Extensions", "MCP, skills, subagents, and adapters.", 140)
EVALS = _area("evals", "Evals", "Deterministic offline evaluation.", 150)
PROJECT_LIFECYCLE = _area(
    "project_lifecycle",
    "Project lifecycle",
    "Boundaries of Studio ownership after export.",
    160,
)

AREAS = (
    EXECUTION,
    PROVIDERS,
    PROMPT,
    RELIABILITY,
    COMPACTION,
    STRUCTURED_OUTPUT,
    COMPLETION,
    TOOLS,
    CONTEXT_RAG,
    MEMORY,
    FILESYSTEM,
    HOOKS,
    OBSERVATION,
    PERSISTENCE,
    PERMISSIONS,
    EXTENSIONS,
    EVALS,
    PROJECT_LIFECYCLE,
)

_Item = tuple[str, str, str]


def _records(
    area: CapabilityArea,
    status: CapabilityStatus,
    items: Sequence[_Item],
) -> tuple[CapabilityRecord, ...]:
    return tuple(
        CapabilityRecord(
            id=f"{area.id}.{identifier}",
            title=title,
            summary=summary,
            area=area,
            status=status,
        )
        for identifier, title, summary in items
    )


def _execution_model(identifier: str, title: str, summary: str) -> CapabilityRecord:
    return CapabilityRecord(
        id=f"execution.{identifier}",
        title=title,
        summary=summary,
        area=EXECUTION,
        status=RUNTIME_READY,
        execution_model=True,
    )


_EXECUTION_MODELS = (
    _execution_model(
        "standard_agent",
        "Standard agent loop",
        "A model-tool agent loop with agent-judged completion and explicit limits.",
    ),
    _execution_model(
        "deep_agent",
        "Deep agent loop",
        "A model-directed deep-agent loop with a shared tree budget.",
    ),
    _execution_model(
        "coordinator",
        "Coordinator loop",
        "A coordinator agent loop with explicit subagents and a shared tree budget.",
    ),
    _execution_model(
        "directed_workflow",
        "Directed workflow",
        "A code-directed, replayable acyclic graph of agent calls.",
    ),
    _execution_model(
        "goal_verified",
        "Goal-verified agent loop",
        "A root agent loop whose verifier feedback decides retry or stop.",
    ),
    _execution_model(
        "routine",
        "Routine",
        "A manual, cron, CI, or webhook wrapper whose host owns process lifetime.",
    ),
)

EXECUTION_MODEL_IDS = tuple(item.id for item in _EXECUTION_MODELS)

CAPABILITIES = (
    *_EXECUTION_MODELS,
    *_records(
        PROVIDERS,
        RUNTIME_READY,
        (
            ("openai_responses", "OpenAI Responses", "OpenAI Responses provider generation."),
            ("openai_chat", "OpenAI Chat", "OpenAI-compatible chat provider generation."),
            ("anthropic", "Anthropic", "Anthropic provider generation."),
            ("gemini", "Gemini", "Gemini provider generation."),
            ("llama_cpp", "llama.cpp", "llama.cpp server provider generation."),
            ("vllm", "vLLM", "vLLM server provider generation."),
            ("sglang", "SGLang", "SGLang server provider generation."),
            ("model_catalog", "Model catalog", "Hand-authored provider model choices."),
            ("thinking", "Thinking", "Provider-supported thinking configuration."),
            ("streaming", "Streaming", "Streaming provider responses."),
            ("fallback_chain", "Fallback chain", "Ordered fallback model configuration."),
        ),
    ),
    *_records(
        PROVIDERS,
        SKELETON_TODO,
        (("custom_adapter", "Custom provider adapter", "A typed provider adapter TODO."),),
    ),
    *_records(
        PROMPT,
        RUNTIME_READY,
        (
            ("append_defaults", "Append defaults", "Append project instructions to defaults."),
            ("replace_defaults", "Replace defaults", "Replace default prompt content."),
            ("ordered_sections", "Ordered sections", "Emit prompt sections in declared order."),
            ("section_placement", "Section placement", "Place sections at supported boundaries."),
            ("cacheable_boundary", "Cacheable boundary", "Mark stable prompt cache boundaries."),
        ),
    ),
    *_records(
        PROMPT,
        SKELETON_TODO,
        (
            (
                "custom_dynamic_policy",
                "Custom dynamic prompt/context policy",
                "A project-owned dynamic prompt or context policy TODO.",
            ),
        ),
    ),
    *_records(
        RELIABILITY,
        RUNTIME_READY,
        (
            ("provider_retry", "Provider retry", "Provider retry and backoff configuration."),
            ("tool_timeout", "Tool timeout", "Agent and tool execution deadlines."),
            ("tool_retry", "Tool retry", "Scope-aware tool retry configuration."),
            ("max_turns", "Maximum turns", "A hard agent turn limit."),
            ("max_output_tokens", "Maximum output", "A provider output token limit."),
            (
                "run_budget",
                "Shared run budget",
                "One token and cost budget shared by the root agent and every subagent.",
            ),
            ("loop_guard", "Loop guard", "Runaway loop and repeated-failure detection."),
            ("truncation_recovery", "Truncation recovery", "Bounded truncated-output recovery."),
        ),
    ),
    *_records(
        RELIABILITY,
        SKELETON_TODO,
        (
            (
                "custom_token_estimator",
                "Custom token estimator",
                "A project-owned token estimation policy TODO.",
            ),
            (
                "custom_recovery_policy",
                "Custom recovery policy",
                "A project-owned recovery strategy TODO.",
            ),
        ),
    ),
    *_records(
        COMPACTION,
        RUNTIME_READY,
        (
            ("default_coding", "Default coding", "Default coding-oriented compaction."),
            ("general_domain", "General domain", "General-domain compaction prompt."),
            ("detailed", "Detailed compaction", "Detailed compaction strategy."),
            ("ladder", "Compaction ladder", "CompactionLadder recovery rungs."),
        ),
    ),
    *_records(
        COMPACTION,
        SKELETON_TODO,
        (("custom_strategy", "Custom compaction strategy", "A custom strategy TODO."),),
    ),
    *_records(
        STRUCTURED_OUTPUT,
        RUNTIME_READY,
        (
            ("json_schema", "JSON Schema", "OutputSchema generation from JSON Schema."),
            ("strict_mode", "Strict mode", "Strict provider schema enforcement."),
            ("final_tool_capture", "Final-tool capture", "Terminal final-tool output capture."),
            ("repair_retries", "Repair retries", "Bounded structured-output repair retries."),
        ),
    ),
    *_records(
        STRUCTURED_OUTPUT,
        SKELETON_TODO,
        (("domain_scorer", "Domain scorer", "A project-owned structured scorer TODO."),),
    ),
    *_records(
        COMPLETION,
        RUNTIME_READY,
        (
            (
                "agent_judged",
                "Agent-judged completion",
                "The root agent decides when its final answer is complete.",
            ),
            (
                "verifier_gated",
                "Verifier-gated completion",
                "Root-only verifier feedback controls bounded retry or final stop.",
            ),
            (
                "text_contains",
                "Text-contains verifier",
                "Generated case-insensitive final-text verification.",
            ),
            (
                "json_schema",
                "JSON Schema verifier",
                "Generated structured-output or final-text JSON Schema verification.",
            ),
        ),
    ),
    *_records(
        COMPLETION,
        SKELETON_TODO,
        (
            (
                "custom_todo",
                "Custom verifier TODO",
                "A generated verifier seam that rejects results until it is implemented.",
            ),
        ),
    ),
    *_records(
        TOOLS,
        RUNTIME_READY,
        (
            ("input_schema", "Input schema", "Explicit JSON input schema generation."),
            ("scope", "Scope", "Read, write, and exec scope generation."),
            ("parallelism", "Parallelism", "Tool parallelism policy generation."),
            ("resources", "Resources", "ResourceAccess declarations."),
            ("retryability", "Retryability", "Mutating-tool retryability declarations."),
            ("contract_test", "Contract test", "Offline Linch tool contract tests."),
        ),
    ),
    *_records(
        TOOLS,
        SKELETON_TODO,
        (
            (
                "function_skeleton",
                "Function tool skeleton",
                "A wired tool seam that fails visibly until its function body is implemented.",
            ),
            (
                "class_skeleton",
                "Class tool skeleton",
                "A wired tool seam that fails visibly until its class behavior is implemented.",
            ),
            (
                "external_api_implementation",
                "External API implementation",
                "An explicit failing TODO for external API behavior.",
            ),
            (
                "database_implementation",
                "Database implementation",
                "An explicit failing TODO for database behavior.",
            ),
        ),
    ),
    *_records(
        CONTEXT_RAG,
        RUNTIME_READY,
        (
            ("injection_hook", "Context injection hook", "ContextInjectionHook wiring."),
            ("budget", "Context budget", "ContextBudget generation."),
            ("memory_recall", "Memory recall", "Memory-backed recall injection."),
        ),
    ),
    *_records(
        CONTEXT_RAG,
        SKELETON_TODO,
        (
            ("custom_builder", "Custom context builder", "A custom builder TODO."),
            (
                "dynamic_tool_selector",
                "Dynamic tool selector",
                "A dynamic per-turn tool selection TODO.",
            ),
        ),
    ),
    *_records(
        MEMORY,
        RUNTIME_READY,
        (
            ("in_memory", "In-memory memory", "InMemoryKeywordMemoryStore generation."),
            ("sqlite", "SQLite memory", "SqliteMemoryStore generation."),
            ("postgres_keyword", "Postgres keyword memory", "Keyword Postgres memory wiring."),
            ("tiered", "Tiered memory", "TieredMemoryStore generation."),
            ("search_tool", "Memory search tool", "MemorySearchTool generation."),
            ("upsert_tool", "Memory upsert tool", "MemoryUpsertTool generation."),
        ),
    ),
    *_records(
        MEMORY,
        SKELETON_TODO,
        (
            ("faiss", "FAISS", "A FAISS MemoryStore adapter TODO and guidance."),
            ("pgvector", "pgvector", "A pgvector MemoryStore adapter TODO and guidance."),
            ("qdrant", "Qdrant", "A Qdrant MemoryStore adapter TODO and guidance."),
            ("custom_store", "Custom MemoryStore", "A custom MemoryStore adapter TODO."),
            (
                "extraction_hook",
                "Memory extraction hook",
                "A generated extraction-hook seam and implementation TODO.",
            ),
        ),
    ),
    *_records(
        FILESYSTEM,
        RUNTIME_READY,
        (
            ("state_backend", "State backend", "StateFileBackend generation."),
            ("disk_backend", "Disk backend", "DiskFileBackend generation."),
            ("sqlite_backend", "SQLite backend", "SqliteFileBackend generation."),
            ("composite_backend", "Composite backend", "CompositeFileBackend generation."),
            ("offload_thresholds", "Offload thresholds", "Result offload thresholds."),
            ("read_before_write", "Read before write", "Read-before-write enforcement."),
        ),
    ),
    *_records(
        FILESYSTEM,
        SKELETON_TODO,
        (
            ("external_backend", "External FileBackend", "An external FileBackend TODO."),
            ("postgres_backend", "Postgres FileBackend", "A Postgres FileBackend TODO."),
        ),
    ),
    *_records(
        HOOKS,
        RUNTIME_READY,
        (
            ("context", "Context hook", "Context hook generation."),
            ("telemetry", "Telemetry hook", "Run telemetry hook generation."),
            ("cache", "Cache hook", "Tool cache hook generation."),
            ("read_before_write", "Read-before-write hook", "Read-before-write hook wiring."),
        ),
    ),
    *_records(
        HOOKS,
        SKELETON_TODO,
        (
            ("custom_hook", "Custom hook", "A custom hook TODO."),
            ("middleware", "Custom middleware", "A custom middleware TODO."),
            ("redaction_policy", "Redaction policy", "A project-owned redaction TODO."),
            (
                "memory_extraction",
                "Memory extraction hook",
                "A generated memory-extraction seam and implementation TODO.",
            ),
            (
                "stop_predicate",
                "Stop predicate",
                "A generated stop-predicate seam and implementation TODO.",
            ),
        ),
    ),
    *_records(
        OBSERVATION,
        RUNTIME_READY,
        (
            ("typed_event_sink", "Typed event sink", "Typed event consumption skeleton."),
            ("logging", "Logging", "LoggingObserver generation."),
            ("otel", "OpenTelemetry", "OpenTelemetry observer generation."),
            ("run_report", "Run report", "RunReport construction and inspection."),
            ("cache_diagnostics", "Cache diagnostics", "Prompt/tool cache diagnostics."),
            (
                "compaction_diagnostics",
                "Compaction diagnostics",
                "Compaction diagnostics in reports.",
            ),
        ),
    ),
    *_records(
        OBSERVATION,
        SKELETON_TODO,
        (("vendor_exporter", "Vendor exporter", "A vendor-specific exporter TODO."),),
    ),
    *_records(
        PERSISTENCE,
        RUNTIME_READY,
        (
            ("in_memory_session", "In-memory sessions", "InMemorySessionStore generation."),
            ("sqlite_session", "SQLite sessions", "SqliteSessionStore generation."),
            ("in_memory_run", "In-memory runs", "InMemoryRunStore generation."),
            ("sqlite_run", "SQLite runs", "SqliteRunStore generation."),
            ("durable_resume", "Durable resume", "Checkpointed run resume wiring."),
        ),
    ),
    *_records(
        PERSISTENCE,
        SKELETON_TODO,
        (
            ("external_session_store", "External SessionStore", "A SessionStore adapter TODO."),
            (
                "external_run_store",
                "External RunStore",
                "A RunStore adapter TODO; no fake Postgres implementation.",
            ),
        ),
    ),
    *_records(
        PERMISSIONS,
        RUNTIME_READY,
        (
            ("default_mode", "Default mode", "Interactive default permission mode."),
            ("accept_edits_mode", "Accept-edits mode", "Accept-edits permission mode."),
            ("trusted_mode", "Trusted mode", "Explicit high-risk trusted permission mode."),
            ("tool_rules", "Tool rules", "Ordered ToolRule generation."),
            ("path_rules", "Path rules", "Ordered PathRule generation."),
            ("bash_rules", "Bash rules", "Ordered BashRule generation."),
        ),
    ),
    *_records(
        PERMISSIONS,
        SKELETON_TODO,
        (
            ("hitl_callback", "HITL callback", "A host-owned approval callback TODO."),
            (
                "organization_policy",
                "Organization policy",
                "A project-owned organization policy TODO.",
            ),
        ),
    ),
    *_records(
        EXTENSIONS,
        RUNTIME_READY,
        (
            ("mcp_stdio", "MCP stdio", "MCP stdio server configuration."),
            ("mcp_http", "MCP HTTP", "MCP HTTP server configuration."),
            ("skill_files", "Skill files", "Generated .linch skill files."),
            ("subagent_files", "Subagent files", "Generated .linch subagent files."),
        ),
    ),
    *_records(
        EXTENSIONS,
        SKELETON_TODO,
        (
            ("live_mcp_discovery", "Live MCP discovery", "A live discovery TODO."),
            ("mailbox_adapter", "Mailbox adapter", "A Mailbox adapter TODO."),
            ("isolation_adapter", "Isolation adapter", "An IsolationBackend adapter TODO."),
            ("schedule_store_adapter", "ScheduleStore adapter", "A ScheduleStore TODO."),
        ),
    ),
    *_records(
        EVALS,
        RUNTIME_READY,
        (
            ("offline_scripted_tests", "Offline scripted tests", "Credential-free scripted tests."),
            ("eval_suites", "Eval suites", "Deterministic eval-suite generation."),
            ("text_scorer", "Text scorer", "Text scorer generation."),
            ("tool_scorer", "Tool scorer", "Tool-call scorer generation."),
            ("schema_scorer", "Schema scorer", "JSON Schema scorer generation."),
            ("cost_scorer", "Cost scorer", "Run-cost scorer generation."),
            ("context_scorer", "Context scorer", "Context behavior scorer generation."),
            ("memory_scorer", "Memory scorer", "Memory behavior scorer generation."),
        ),
    ),
    *_records(
        EVALS,
        SKELETON_TODO,
        (("domain_scorer", "Domain scorer", "A domain-specific scorer TODO."),),
    ),
    *_records(
        EXECUTION,
        UNSUPPORTED,
        (
            (
                "arbitrary_branch_nodes",
                "Arbitrary branch nodes",
                "Arbitrary workflow branching is deferred.",
            ),
            ("condition_nodes", "Condition nodes", "Conditional graph nodes are deferred."),
            ("retry_nodes", "Retry graph nodes", "Arbitrary graph retry nodes are deferred."),
            (
                "direct_tool_nodes",
                "Direct tool nodes",
                "Directed workflows accept agent-call steps only.",
            ),
            (
                "deep_agent_control_edges",
                "Deep-agent control-flow edges",
                "Deep agents are model-directed loops, not code-directed graphs.",
            ),
            (
                "agent_self_scheduling",
                "Agent self-scheduling",
                "Routine scheduling and process lifetime belong to the host.",
            ),
            (
                "hosted_execution_deployment",
                "Hosted execution/deployment",
                "Studio does not run or deploy generated projects.",
            ),
        ),
    ),
    *_records(
        PROJECT_LIFECYCLE,
        UNSUPPORTED,
        (
            (
                "code_to_blueprint_import",
                "Code-to-blueprint import",
                "Studio does not infer a graph from Python code.",
            ),
            (
                "bidirectional_sync",
                "Bidirectional synchronization",
                "Exported code is the source of truth after one-way export.",
            ),
        ),
    ),
    *_records(
        EXTENSIONS,
        UNSUPPORTED,
        (
            (
                "marketplace_plugin_installation",
                "Marketplace/plugin installation",
                "Studio does not install marketplace plugins.",
            ),
            (
                "multi_user_collaboration",
                "Multi-user collaboration",
                "Studio is local and single-user.",
            ),
        ),
    ),
)

RELATION_MATRIX_VERSION = RelationMatrixVersion(
    api_version="studio.linch.dev/relation-matrix/v1alpha2",
    revision=2,
    blueprint_api_version="studio.linch.dev/v1alpha2",
)


def _relation(
    identifier: str,
    source_kind: RelationEndpointKind,
    target_kind: RelationEndpointKind,
    relation: str,
    decision: RelationDecision,
    *,
    field: str | None,
    constraints: tuple[RelationConstraint, ...] = (),
    reason: str,
    order: int,
) -> EditableRelation:
    return EditableRelation(
        id=identifier,
        source_kind=source_kind,
        target_kind=target_kind,
        relation=relation,
        decision=decision,
        field=field,
        constraints=constraints,
        reason=reason,
        order=order,
    )


EDITABLE_RELATIONS = (
    _relation(
        "workflow_step_depends_on_workflow_step",
        "workflow_step",
        "workflow_step",
        "depends_on",
        "allow",
        field="dependsOn",
        constraints=("same_workflow", "acyclic"),
        reason="A workflow step may depend on an earlier step in the same directed workflow.",
        order=0,
    ),
    _relation(
        "subagent_binds_workflow_step",
        "subagent",
        "workflow_step",
        "subagent_binding",
        "allow",
        field="subagent",
        constraints=("max_one_per_target",),
        reason="An agent-call step accepts at most one subagent binding.",
        order=10,
    ),
    _relation(
        "tool_filters_agent_loop",
        "tool",
        "agent_loop",
        "tool_filter",
        "allow",
        field="tools",
        reason="A tool attachment edits the primary runtime agent tool allowlist.",
        order=20,
    ),
    _relation(
        "tool_filters_subagent",
        "tool",
        "subagent",
        "tool_filter",
        "allow",
        field="tools",
        reason="A tool attachment edits the subagent tool allowlist.",
        order=30,
    ),
    _relation(
        "tool_filters_workflow_step",
        "tool",
        "workflow_step",
        "tool_filter",
        "allow",
        field="tools",
        reason="A tool attachment edits the agent-call step tool filter.",
        order=40,
    ),
    _relation(
        "tool_allows_skill",
        "tool",
        "skill",
        "allowed_tool",
        "allow",
        field="allowedTools",
        reason="A tool attachment edits the skill allowed-tools list.",
        order=50,
    ),
    _relation(
        "trigger_invokes_routine",
        "trigger",
        "routine",
        "trigger_binding",
        "allow",
        field="triggers",
        reason="A trigger may invoke a routine; the host still owns delivery and lifetime.",
        order=60,
    ),
    _relation(
        "agent_tick_routine_targets_agent_loop",
        "agent_tick_routine",
        "agent_loop",
        "routine_target",
        "allow",
        field="target",
        reason="An agent-tick routine invokes one bounded agent-loop tick.",
        order=70,
    ),
    _relation(
        "workflow_run_routine_targets_directed_workflow",
        "workflow_run_routine",
        "directed_workflow",
        "routine_target",
        "allow",
        field="target",
        reason="A workflow-run routine invokes its directed workflow directly.",
        order=80,
    ),
    _relation(
        "agent_loop_direct_control_forbidden",
        "agent_loop",
        "agent_loop",
        "direct_control",
        "deny",
        field=None,
        reason="Agent-to-agent control edges are not a Blueprint relation.",
        order=90,
    ),
    _relation(
        "hook_to_agent_forbidden",
        "hook",
        "agent_loop",
        "hook_attachment",
        "deny",
        field=None,
        reason="Hooks are inspector configuration, not canvas control-flow nodes.",
        order=100,
    ),
    _relation(
        "hook_to_hook_forbidden",
        "hook",
        "hook",
        "hook_chain",
        "deny",
        field=None,
        reason="Hook ordering is configuration and cannot be authored as a canvas edge.",
        order=110,
    ),
    _relation(
        "cross_workflow_flow_forbidden",
        "workflow_step",
        "workflow_step",
        "cross_workflow_flow",
        "deny",
        field=None,
        reason="Control-flow dependencies cannot cross directed-workflow boundaries.",
        order=120,
    ),
    _relation(
        "direct_tool_step_forbidden",
        "tool",
        "workflow_step",
        "direct_tool_call",
        "deny",
        field=None,
        reason="Directed workflows contain agent-call steps; tools are filters, not steps.",
        order=130,
    ),
    _relation(
        "agent_tick_routine_to_workflow_forbidden",
        "agent_tick_routine",
        "directed_workflow",
        "routine_target",
        "deny",
        field=None,
        reason="Agent-tick routines can target only agent loops.",
        order=140,
    ),
    _relation(
        "workflow_run_routine_to_agent_forbidden",
        "workflow_run_routine",
        "agent_loop",
        "routine_target",
        "deny",
        field=None,
        reason="Workflow-run routines can target only directed workflows.",
        order=150,
    ),
)

RELATION_MATRIX = EditableRelationMatrix(
    version=RELATION_MATRIX_VERSION,
    relations=EDITABLE_RELATIONS,
)

CATALOG = CapabilityCatalog(
    version=CATALOG_VERSION,
    statuses=STATUSES,
    areas=AREAS,
    capabilities=CAPABILITIES,
    relation_matrix=RELATION_MATRIX,
)

_CAPABILITY_BY_ID = {item.id: item for item in CAPABILITIES}
_STATUS_BY_ID = {item.id: item for item in STATUSES}


def get_capability(capability_id: str) -> CapabilityRecord:
    """Return one catalog record or raise a descriptive ``KeyError``."""

    try:
        return _CAPABILITY_BY_ID[capability_id]
    except KeyError:
        raise KeyError(f"unknown capability id: {capability_id}") from None


def badge_for_capability(capability_id: str) -> CapabilityBadge:
    """Project one capability to the badge shape consumed by the UI."""

    capability = get_capability(capability_id)
    return CapabilityBadge(
        capability_id=capability.id,
        status=capability.status.id,
        label=capability.status.badge,
        export_allowed=capability.status.export_allowed,
    )


def badges_for_selection(capability_ids: Iterable[str]) -> tuple[CapabilityBadge, ...]:
    """Return deduplicated badges in catalog order, independent of input ordering."""

    if isinstance(capability_ids, str):
        raise TypeError("capability_ids must be an iterable of ids, not a string")
    selected = set(capability_ids)
    unknown = sorted(selected.difference(_CAPABILITY_BY_ID))
    if unknown:
        raise KeyError(f"unknown capability ids: {', '.join(unknown)}")
    return tuple(
        badge_for_capability(capability.id)
        for capability in CAPABILITIES
        if capability.id in selected
    )


def status_for_selection(capability_ids: Iterable[str]) -> CapabilityStatus | None:
    """Return the least-ready selected status, or ``None`` for an empty selection."""

    badges = badges_for_selection(capability_ids)
    if not badges:
        return None
    return max((_STATUS_BY_ID[badge.status] for badge in badges), key=lambda item: item.order)


def catalog_document() -> dict[str, object]:
    """Return a fresh JSON-ready catalog document with stable list ordering."""

    return CATALOG.to_dict()


def catalog_json() -> str:
    """Return canonical compact JSON for caching and drift checks."""

    return json.dumps(catalog_document(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)


__all__ = [
    "AREAS",
    "CAPABILITIES",
    "CATALOG",
    "CATALOG_VERSION",
    "EDITABLE_RELATIONS",
    "EXECUTION_MODEL_IDS",
    "RELATION_MATRIX",
    "RELATION_MATRIX_VERSION",
    "RUNTIME_READY",
    "SKELETON_TODO",
    "STATUSES",
    "UNSUPPORTED",
    "badge_for_capability",
    "badges_for_selection",
    "catalog_document",
    "catalog_json",
    "get_capability",
    "status_for_selection",
]
