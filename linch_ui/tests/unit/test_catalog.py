from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

from linch_studio.catalog import (
    AREAS,
    CATALOG,
    EDITABLE_RELATIONS,
    EXECUTION_MODEL_IDS,
    RELATION_MATRIX,
    RUNTIME_READY,
    SKELETON_TODO,
    STATUSES,
    UNSUPPORTED,
    badge_for_capability,
    badges_for_selection,
    catalog_document,
    catalog_json,
    get_capability,
    status_for_selection,
)

EXPECTED_RUNTIME_READY = {
    "execution.coordinator",
    "execution.deep_agent",
    "execution.directed_workflow",
    "execution.goal_verified",
    "execution.routine",
    "execution.standard_agent",
    "providers.anthropic",
    "providers.fallback_chain",
    "providers.gemini",
    "providers.llama_cpp",
    "providers.model_catalog",
    "providers.openai_chat",
    "providers.openai_responses",
    "providers.sglang",
    "providers.streaming",
    "providers.thinking",
    "providers.vllm",
    "prompt.append_defaults",
    "prompt.cacheable_boundary",
    "prompt.ordered_sections",
    "prompt.replace_defaults",
    "prompt.section_placement",
    "reliability.loop_guard",
    "reliability.max_output_tokens",
    "reliability.max_turns",
    "reliability.provider_retry",
    "reliability.run_budget",
    "reliability.tool_retry",
    "reliability.tool_timeout",
    "reliability.truncation_recovery",
    "compaction.default_coding",
    "compaction.detailed",
    "compaction.general_domain",
    "compaction.ladder",
    "structured_output.final_tool_capture",
    "structured_output.json_schema",
    "structured_output.repair_retries",
    "structured_output.strict_mode",
    "completion.agent_judged",
    "completion.json_schema",
    "completion.text_contains",
    "completion.verifier_gated",
    "tools.contract_test",
    "tools.input_schema",
    "tools.parallelism",
    "tools.resources",
    "tools.retryability",
    "tools.scope",
    "context_rag.budget",
    "context_rag.injection_hook",
    "context_rag.memory_recall",
    "memory.in_memory",
    "memory.postgres_keyword",
    "memory.search_tool",
    "memory.sqlite",
    "memory.tiered",
    "memory.upsert_tool",
    "filesystem.composite_backend",
    "filesystem.disk_backend",
    "filesystem.offload_thresholds",
    "filesystem.read_before_write",
    "filesystem.sqlite_backend",
    "filesystem.state_backend",
    "hooks.cache",
    "hooks.context",
    "hooks.read_before_write",
    "hooks.telemetry",
    "observation.cache_diagnostics",
    "observation.compaction_diagnostics",
    "observation.logging",
    "observation.otel",
    "observation.run_report",
    "observation.typed_event_sink",
    "persistence.durable_resume",
    "persistence.in_memory_run",
    "persistence.in_memory_session",
    "persistence.sqlite_run",
    "persistence.sqlite_session",
    "permissions.accept_edits_mode",
    "permissions.bash_rules",
    "permissions.default_mode",
    "permissions.path_rules",
    "permissions.tool_rules",
    "permissions.trusted_mode",
    "extensions.mcp_http",
    "extensions.mcp_stdio",
    "extensions.skill_files",
    "extensions.subagent_files",
    "evals.context_scorer",
    "evals.cost_scorer",
    "evals.eval_suites",
    "evals.memory_scorer",
    "evals.offline_scripted_tests",
    "evals.schema_scorer",
    "evals.text_scorer",
    "evals.tool_scorer",
}

EXPECTED_SKELETON_TODO = {
    "providers.custom_adapter",
    "prompt.custom_dynamic_policy",
    "reliability.custom_recovery_policy",
    "reliability.custom_token_estimator",
    "compaction.custom_strategy",
    "structured_output.domain_scorer",
    "completion.custom_todo",
    "tools.class_skeleton",
    "tools.database_implementation",
    "tools.external_api_implementation",
    "tools.function_skeleton",
    "context_rag.custom_builder",
    "context_rag.dynamic_tool_selector",
    "memory.custom_store",
    "memory.extraction_hook",
    "memory.faiss",
    "memory.pgvector",
    "memory.qdrant",
    "filesystem.external_backend",
    "filesystem.postgres_backend",
    "hooks.custom_hook",
    "hooks.memory_extraction",
    "hooks.middleware",
    "hooks.redaction_policy",
    "hooks.stop_predicate",
    "observation.vendor_exporter",
    "persistence.external_run_store",
    "persistence.external_session_store",
    "permissions.hitl_callback",
    "permissions.organization_policy",
    "extensions.isolation_adapter",
    "extensions.live_mcp_discovery",
    "extensions.mailbox_adapter",
    "extensions.schedule_store_adapter",
    "evals.domain_scorer",
}

EXPECTED_UNSUPPORTED = {
    "execution.agent_self_scheduling",
    "execution.arbitrary_branch_nodes",
    "execution.condition_nodes",
    "execution.deep_agent_control_edges",
    "execution.direct_tool_nodes",
    "execution.hosted_execution_deployment",
    "execution.retry_nodes",
    "extensions.marketplace_plugin_installation",
    "extensions.multi_user_collaboration",
    "project_lifecycle.bidirectional_sync",
    "project_lifecycle.code_to_blueprint_import",
}

EXPECTED_RELATION_IDS = (
    "workflow_step_depends_on_workflow_step",
    "subagent_binds_workflow_step",
    "tool_filters_agent_loop",
    "tool_filters_subagent",
    "tool_filters_workflow_step",
    "tool_allows_skill",
    "trigger_invokes_routine",
    "agent_tick_routine_targets_agent_loop",
    "workflow_run_routine_targets_directed_workflow",
    "agent_loop_direct_control_forbidden",
    "hook_to_agent_forbidden",
    "hook_to_hook_forbidden",
    "cross_workflow_flow_forbidden",
    "direct_tool_step_forbidden",
    "agent_tick_routine_to_workflow_forbidden",
    "workflow_run_routine_to_agent_forbidden",
)


def _ids_for(status: str) -> set[str]:
    return {item.id for item in CATALOG.capabilities if item.status.id == status}


def test_catalog_covers_the_hand_authored_support_matrix() -> None:
    assert _ids_for("runtime_ready") == EXPECTED_RUNTIME_READY
    assert _ids_for("skeleton_todo") == EXPECTED_SKELETON_TODO
    assert _ids_for("unsupported") == EXPECTED_UNSUPPORTED


def test_catalog_has_exactly_the_v1alpha2_run_shapes() -> None:
    assert EXECUTION_MODEL_IDS == (
        "execution.standard_agent",
        "execution.deep_agent",
        "execution.coordinator",
        "execution.directed_workflow",
        "execution.goal_verified",
        "execution.routine",
    )
    execution_models = [item for item in CATALOG.capabilities if item.execution_model]
    assert tuple(item.id for item in execution_models) == EXECUTION_MODEL_IDS
    assert {item.status for item in execution_models} == {RUNTIME_READY}


def test_catalog_metadata_and_ids_are_stable_and_unique() -> None:
    assert CATALOG.version.api_version == "studio.linch.dev/catalog/v1alpha2"
    assert CATALOG.version.revision == 2
    assert CATALOG.version.target_linch == ">=1.1,<2"
    assert CATALOG.version.source == "hand_authored"

    assert [area.order for area in AREAS] == sorted(area.order for area in AREAS)
    assert len({area.id for area in AREAS}) == len(AREAS)
    ids = [item.id for item in CATALOG.capabilities]
    assert len(ids) == len(set(ids))


def test_status_contract_is_stable_and_unsupported_alone_locks_export() -> None:
    assert [status.to_dict() for status in STATUSES] == [
        {
            "id": "runtime_ready",
            "badge": "Runtime-ready",
            "description": "Studio emits complete Linch wiring for this capability.",
            "order": 0,
            "exportAllowed": True,
        },
        {
            "id": "skeleton_todo",
            "badge": "Skeleton/TODO",
            "description": (
                "Studio emits an explicit implementation seam and visible TODO; runtime "
                "behavior remains blocked until it is implemented."
            ),
            "order": 1,
            "exportAllowed": True,
        },
        {
            "id": "unsupported",
            "badge": "Unsupported",
            "description": "Studio rejects this design and does not emit approximate code.",
            "order": 2,
            "exportAllowed": False,
        },
    ]


def test_relation_matrix_is_versioned_unique_and_stably_ordered() -> None:
    assert RELATION_MATRIX.version.to_dict() == {
        "apiVersion": "studio.linch.dev/relation-matrix/v1alpha2",
        "revision": 2,
        "blueprintApiVersion": "studio.linch.dev/v1alpha2",
    }
    assert tuple(relation.id for relation in EDITABLE_RELATIONS) == EXPECTED_RELATION_IDS
    assert [relation.order for relation in EDITABLE_RELATIONS] == list(range(0, 160, 10))
    assert len({relation.id for relation in EDITABLE_RELATIONS}) == len(EDITABLE_RELATIONS)
    assert len(
        {
            (
                relation.source_kind,
                relation.target_kind,
                relation.relation,
            )
            for relation in EDITABLE_RELATIONS
        }
    ) == len(EDITABLE_RELATIONS)


def test_relation_matrix_covers_allowed_mutations_and_explicit_forbidden_edges() -> None:
    relations = {relation.id: relation for relation in EDITABLE_RELATIONS}

    depends_on = relations["workflow_step_depends_on_workflow_step"]
    assert depends_on.field == "dependsOn"
    assert depends_on.constraints == ("same_workflow", "acyclic")
    assert relations["subagent_binds_workflow_step"].constraints == ("max_one_per_target",)
    assert relations["tool_filters_agent_loop"].field == "tools"
    assert relations["tool_filters_subagent"].field == "tools"
    assert relations["tool_filters_workflow_step"].field == "tools"
    assert relations["tool_allows_skill"].field == "allowedTools"
    assert relations["trigger_invokes_routine"].field == "triggers"
    assert relations["agent_tick_routine_targets_agent_loop"].field == "target"
    assert relations["workflow_run_routine_targets_directed_workflow"].field == "target"

    forbidden = [relation for relation in EDITABLE_RELATIONS if relation.decision == "deny"]
    assert {relation.id for relation in forbidden} == {
        "agent_loop_direct_control_forbidden",
        "hook_to_agent_forbidden",
        "hook_to_hook_forbidden",
        "cross_workflow_flow_forbidden",
        "direct_tool_step_forbidden",
        "agent_tick_routine_to_workflow_forbidden",
        "workflow_run_routine_to_agent_forbidden",
    }
    assert all(relation.field is None for relation in forbidden)


def test_catalog_document_is_fresh_deterministic_and_json_ready() -> None:
    first = catalog_document()
    second = catalog_document()

    assert first == second
    assert first is not second
    assert json.loads(catalog_json()) == first
    assert catalog_json() == catalog_json()
    json.dumps(first, allow_nan=False)

    capabilities = first["capabilities"]
    assert isinstance(capabilities, list)
    capabilities.clear()
    assert len(catalog_document()["capabilities"]) == len(CATALOG.capabilities)  # type: ignore[arg-type]
    relation_matrix = first["relationMatrix"]
    assert isinstance(relation_matrix, dict)
    relations = relation_matrix["relations"]
    assert isinstance(relations, list)
    relations.clear()
    fresh_matrix = catalog_document()["relationMatrix"]
    assert isinstance(fresh_matrix, dict)
    assert len(fresh_matrix["relations"]) == len(EDITABLE_RELATIONS)  # type: ignore[arg-type]


def test_badge_helpers_are_deduplicated_and_catalog_ordered() -> None:
    selected = [
        "execution.direct_tool_nodes",
        "providers.custom_adapter",
        "providers.openai_responses",
        "providers.custom_adapter",
    ]
    badges = badges_for_selection(selected)

    assert [badge.capability_id for badge in badges] == [
        "providers.openai_responses",
        "providers.custom_adapter",
        "execution.direct_tool_nodes",
    ]
    assert [badge.label for badge in badges] == [
        "Runtime-ready",
        "Skeleton/TODO",
        "Unsupported",
    ]
    assert [badge.export_allowed for badge in badges] == [True, True, False]
    assert [badge.to_dict()["status"] for badge in badges] == [
        "runtime_ready",
        "skeleton_todo",
        "unsupported",
    ]
    assert status_for_selection(selected) is UNSUPPORTED
    assert status_for_selection(["providers.custom_adapter"]) is SKELETON_TODO
    assert status_for_selection(["providers.openai_responses"]) is RUNTIME_READY
    assert status_for_selection([]) is None


def test_single_badge_and_lookup_helpers() -> None:
    capability = get_capability("providers.openai_responses")
    badge = badge_for_capability(capability.id)

    assert capability.status is RUNTIME_READY
    assert badge.to_dict() == {
        "capabilityId": "providers.openai_responses",
        "status": "runtime_ready",
        "label": "Runtime-ready",
        "exportAllowed": True,
    }

    with pytest.raises(KeyError, match="unknown capability id"):
        get_capability("providers.missing")
    with pytest.raises(KeyError, match="alpha, zeta"):
        badges_for_selection(["zeta", "alpha"])
    with pytest.raises(TypeError, match="iterable of ids"):
        badges_for_selection("providers.openai_responses")


def test_catalog_records_are_immutable() -> None:
    with pytest.raises(FrozenInstanceError):
        CATALOG.version.revision = 2  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        RELATION_MATRIX.version.revision = 2  # type: ignore[misc]
