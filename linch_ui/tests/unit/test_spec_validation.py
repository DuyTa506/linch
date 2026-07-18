from __future__ import annotations

from copy import deepcopy
from typing import Any

from linch_studio.spec import Blueprint, default_blueprint, validate_blueprint


def _merge(target: dict[str, Any], update: dict[str, Any]) -> None:
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = deepcopy(value)


def _blueprint(update: dict[str, Any] | None = None) -> Blueprint:
    data = default_blueprint("demo").model_dump(mode="json", by_alias=True)
    data["spec"]["runtime"]["provider"] = {
        "kind": "openai_responses",
        "model": "gpt-5",
    }
    if update:
        _merge(data, update)
    return Blueprint.model_validate(data, strict=True)


def _codes(blueprint: Blueprint) -> set[str]:
    return {item.code for item in validate_blueprint(blueprint)}


def _tool(identifier: str, *, scope: str = "read") -> dict[str, Any]:
    return {
        "kind": "function",
        "id": identifier,
        "displayName": identifier.title(),
        "description": "A test tool.",
        "scope": scope,
    }


def _worker(identifier: str = "worker") -> dict[str, Any]:
    return {
        "id": identifier,
        "displayName": "Worker",
        "instructions": "Complete one bounded task.",
    }


def _agent_tick(**update: Any) -> dict[str, Any]:
    routine = {
        "kind": "agent_tick",
        "id": "tick",
        "displayName": "Tick",
        "charter": "Maintain the project.",
        "prompt": "Do one task.",
        "target": "runtime_agent",
    }
    routine.update(update)
    return routine


def test_minimal_standard_blueprint_is_semantically_valid() -> None:
    assert validate_blueprint(_blueprint()) == ()


def test_duplicate_keyword_and_reserved_identifiers_are_diagnostics() -> None:
    blueprint = _blueprint(
        {"spec": {"package": "agent", "tools": [_tool("class"), _tool("class")]}}
    )
    diagnostics = validate_blueprint(blueprint)

    assert {item.code for item in diagnostics} >= {
        "semantic.duplicate_id",
        "semantic.python_keyword",
        "semantic.reserved_module_name",
    }
    assert any(item.path == "/spec/tools/1/id" for item in diagnostics)


def test_generated_component_and_verifier_ids_are_globally_unique() -> None:
    blueprint = _blueprint(
        {
            "spec": {
                "tools": [_tool("shared")],
                "workflows": [
                    {
                        "kind": "directed",
                        "id": "shared",
                        "displayName": "Flow",
                        "nodes": [
                            {
                                "type": "agent_call",
                                "id": "step",
                                "label": "Step",
                                "prompt": "Work.",
                            }
                        ],
                        "output": "step",
                    }
                ],
                "routines": [
                    _agent_tick(
                        id="routine",
                        verify={"kind": "text_contains", "id": "shared", "text": "DONE"},
                    )
                ],
            }
        }
    )
    collisions = [
        item
        for item in validate_blueprint(blueprint)
        if item.code == "semantic.component_id_collision"
    ]

    assert {item.path for item in collisions} == {
        "/spec/workflows/0/id",
        "/spec/routines/0/verify/id",
    }


def test_missing_tool_subagent_workflow_and_trigger_references_are_reported() -> None:
    blueprint = _blueprint(
        {
            "spec": {
                "subagents": [{**_worker(), "tools": ["missing_tool"]}],
                "skills": [
                    {
                        "id": "review",
                        "displayName": "Review",
                        "instructions": "Review the result.",
                        "allowedTools": ["missing_tool"],
                    }
                ],
                "workflows": [
                    {
                        "kind": "directed",
                        "id": "flow",
                        "displayName": "Flow",
                        "nodes": [
                            {
                                "type": "agent_call",
                                "id": "step",
                                "label": "Step",
                                "prompt": "Work.",
                                "subagent": "missing_worker",
                                "tools": ["missing_tool"],
                                "dependsOn": ["missing_node"],
                            }
                        ],
                        "output": "missing_output",
                    }
                ],
                "routines": [
                    {
                        "kind": "workflow_run",
                        "id": "run_flow",
                        "displayName": "Run Flow",
                        "target": "missing_flow",
                        "triggers": ["missing_trigger"],
                    }
                ],
            }
        }
    )

    missing = [
        item for item in validate_blueprint(blueprint) if item.code == "semantic.missing_reference"
    ]
    assert len(missing) == 8


def test_runtime_agent_tool_allowlist_validates_references_and_duplicates() -> None:
    blueprint = _blueprint(
        {
            "spec": {
                "runtime": {"agent": {"tools": ["lookup", "missing_tool", "lookup"]}},
                "tools": [_tool("lookup")],
            }
        }
    )
    diagnostics = validate_blueprint(blueprint)

    assert any(
        item.code == "semantic.missing_reference" and item.path == "/spec/runtime/agent/tools/1"
        for item in diagnostics
    )
    assert any(
        item.code == "semantic.duplicate_reference" and item.path == "/spec/runtime/agent/tools/2"
        for item in diagnostics
    )


def test_child_workflow_and_skill_filters_cannot_restore_excluded_runtime_tools() -> None:
    blueprint = _blueprint(
        {
            "spec": {
                "runtime": {"agent": {"tools": []}},
                "tools": [_tool("lookup")],
                "subagents": [{**_worker(), "tools": ["lookup"]}],
                "skills": [
                    {
                        "id": "review",
                        "displayName": "Review",
                        "instructions": "Review the result.",
                        "allowedTools": ["lookup"],
                    }
                ],
                "workflows": [
                    {
                        "kind": "directed",
                        "id": "flow",
                        "displayName": "Flow",
                        "nodes": [
                            {
                                "type": "agent_call",
                                "id": "step",
                                "label": "Step",
                                "prompt": "Work.",
                                "tools": ["lookup"],
                            }
                        ],
                        "output": "step",
                    }
                ],
            }
        }
    )

    diagnostics = [
        item
        for item in validate_blueprint(blueprint)
        if item.code == "semantic.tool_outside_runtime_pool"
    ]
    assert {item.path for item in diagnostics} == {
        "/spec/subagents/0/tools/0",
        "/spec/skills/0/allowedTools/0",
        "/spec/workflows/0/nodes/0/tools/0",
    }


def test_workflow_requires_output_rejects_cycles_and_direct_tools() -> None:
    blueprint = _blueprint(
        {
            "spec": {
                "tools": [_tool("lookup")],
                "workflows": [
                    {
                        "kind": "directed",
                        "id": "cycle",
                        "displayName": "Cycle",
                        "nodes": [
                            {
                                "type": "agent_call",
                                "id": "a",
                                "label": "A",
                                "prompt": "A.",
                                "dependsOn": ["b"],
                            },
                            {
                                "type": "direct_tool",
                                "id": "b",
                                "label": "B",
                                "tool": "lookup",
                                "dependsOn": ["a"],
                            },
                        ],
                    }
                ],
            }
        }
    )

    assert _codes(blueprint) >= {
        "semantic.direct_tool_unsupported",
        "semantic.workflow_cycle",
        "semantic.workflow_output_required",
    }


def test_workflow_requires_one_phase_label_per_topological_depth() -> None:
    blueprint = _blueprint(
        {
            "spec": {
                "workflows": [
                    {
                        "kind": "directed",
                        "id": "parallel",
                        "displayName": "Parallel",
                        "nodes": [
                            {
                                "type": "agent_call",
                                "id": "a",
                                "label": "A",
                                "prompt": "A.",
                                "phase": "research",
                            },
                            {
                                "type": "agent_call",
                                "id": "b",
                                "label": "B",
                                "prompt": "B.",
                                "phase": "verify",
                            },
                        ],
                        "output": "a",
                    }
                ]
            }
        }
    )
    conflicts = [
        item
        for item in validate_blueprint(blueprint)
        if item.code == "semantic.workflow_phase_conflict"
    ]
    assert {item.path for item in conflicts} == {
        "/spec/workflows/0/nodes/0/phase",
        "/spec/workflows/0/nodes/1/phase",
    }


def test_coordinator_inherits_runtime_limits_without_worker_limit_claims() -> None:
    blueprint = _blueprint(
        {
            "spec": {
                "runtime": {
                    "agent": {
                        "preset": "coordinator",
                        "maxTurns": None,
                        "budget": {"maxTokens": None, "maxCostUsd": None},
                    }
                },
                "subagents": [_worker()],
            }
        }
    )

    codes = _codes(blueprint)
    assert codes >= {
        "semantic.autonomous_budget_required",
        "semantic.autonomous_max_turns_required",
    }
    assert not any("worker_" in code for code in codes)


def test_completion_modes_require_consistent_verifiers_and_limits() -> None:
    agent_judged = _blueprint(
        {
            "spec": {
                "runtime": {
                    "agent": {
                        "completion": {
                            "mode": "agent_judged",
                            "verifiers": [{"kind": "text_contains", "id": "done", "text": "DONE"}],
                        }
                    }
                }
            }
        }
    )
    gated = _blueprint(
        {
            "spec": {
                "runtime": {
                    "agent": {
                        "maxTurns": None,
                        "budget": {"maxTokens": None, "maxCostUsd": None},
                        "completion": {"mode": "verifier_gated", "verifiers": []},
                    }
                }
            }
        }
    )

    assert "semantic.agent_judged_verifiers_forbidden" in _codes(agent_judged)
    assert _codes(gated) >= {
        "semantic.completion_limit_required",
        "semantic.completion_verifier_required",
    }


def test_custom_todo_verifier_is_a_visible_non_blocking_schema_warning() -> None:
    blueprint = _blueprint(
        {
            "spec": {
                "runtime": {
                    "agent": {
                        "completion": {
                            "mode": "verifier_gated",
                            "verifiers": [
                                {
                                    "kind": "custom_todo",
                                    "id": "acceptance",
                                    "description": "Implement this blocking seam.",
                                }
                            ],
                        }
                    }
                }
            }
        }
    )
    warnings = [item for item in validate_blueprint(blueprint) if item.severity == "warning"]

    assert [item.code for item in warnings] == ["semantic.skeleton_todo"]


def test_cron_headless_permissions_limits_and_persistence_are_validated() -> None:
    blueprint = _blueprint(
        {
            "spec": {
                "runtime": {
                    "agent": {
                        "maxTurns": None,
                        "budget": {"maxTokens": None, "maxCostUsd": None},
                    }
                },
                "tools": [_tool("execute", scope="exec")],
                "triggers": [
                    {
                        "kind": "cron",
                        "id": "nightly",
                        "displayName": "Nightly",
                        "cron": "99 * * * *",
                        "timezone": "Asia/Ho_Chi_Minh",
                    }
                ],
                "routines": [_agent_tick(triggers=["nightly"])],
                "capabilities": {"persistence": {"durableResume": True}},
            }
        }
    )

    assert _codes(blueprint) >= {
        "semantic.headless_limit_required",
        "semantic.headless_permissions",
        "semantic.invalid_cron",
        "semantic.persistence_claim_invalid",
    }


def test_unconditional_tool_policy_prevents_headless_approval_pause() -> None:
    blueprint = _blueprint(
        {
            "spec": {
                "tools": [_tool("execute", scope="exec")],
                "triggers": [
                    {
                        "kind": "cron",
                        "id": "nightly",
                        "displayName": "Nightly",
                        "cron": "0 0 * * *",
                    }
                ],
                "routines": [_agent_tick(triggers=["nightly"], maxTurns=3)],
                "capabilities": {
                    "permissions": {
                        "rules": [{"kind": "tool", "tool": "execute", "decision": "deny"}]
                    }
                },
            }
        }
    )

    assert "semantic.headless_permissions" not in _codes(blueprint)


def test_workflow_run_routine_targets_a_directed_workflow() -> None:
    blueprint = _blueprint(
        {
            "spec": {
                "workflows": [
                    {
                        "kind": "directed",
                        "id": "flow",
                        "displayName": "Flow",
                        "nodes": [
                            {
                                "type": "agent_call",
                                "id": "step",
                                "label": "Step",
                                "prompt": "Do one step.",
                            }
                        ],
                        "output": "step",
                    }
                ],
                "routines": [
                    {
                        "kind": "workflow_run",
                        "id": "run_flow",
                        "displayName": "Run Flow",
                        "target": "flow",
                    }
                ],
            }
        }
    )

    assert validate_blueprint(blueprint) == ()


def test_webhook_signature_reference_is_required() -> None:
    blueprint = _blueprint(
        {
            "spec": {
                "triggers": [{"kind": "webhook", "id": "incoming", "displayName": "Incoming"}],
                "routines": [_agent_tick(triggers=["incoming"])],
            }
        }
    )

    assert "semantic.webhook_signing_secret_required" in _codes(blueprint)


def test_unsupported_and_inconsistent_capabilities_block_export() -> None:
    blueprint = _blueprint(
        {
            "spec": {
                "capabilities": {
                    "extensions": {"liveMcpDiscovery": True},
                    "context": {"memoryRecall": True},
                    "structuredOutput": {"enabled": False, "repairRetries": 1},
                    "externalDatabase": {
                        "enabled": False,
                        "adapterTemplates": ["memory_store"],
                    },
                }
            }
        }
    )

    assert _codes(blueprint) >= {
        "semantic.external_database_disabled",
        "semantic.memory_backend_required",
        "semantic.structured_output_disabled",
        "semantic.unsupported_capability",
    }


def test_skeleton_capabilities_warn_but_do_not_become_unsupported() -> None:
    blueprint = _blueprint(
        {
            "spec": {
                "runtime": {"provider": {"kind": "custom", "model": "local-model"}},
                "capabilities": {
                    "prompt": {"customDynamicPolicy": True},
                    "memory": {"backend": "qdrant"},
                },
            }
        }
    )
    diagnostics = validate_blueprint(blueprint)

    skeletons = [item for item in diagnostics if item.code == "semantic.skeleton_todo"]
    assert len(skeletons) == 3
    assert all(item.severity == "warning" for item in skeletons)
    assert not any(item.code == "semantic.unsupported_capability" for item in diagnostics)


def test_database_writes_require_scope_idempotency_and_schema() -> None:
    missing = _blueprint(
        {
            "spec": {
                "tools": [
                    {
                        "kind": "database",
                        "id": "save_record",
                        "displayName": "Save Record",
                        "description": "Write one record.",
                        "operation": "write",
                        "scope": "read",
                    }
                ]
            }
        }
    )
    malformed = _blueprint(
        {
            "spec": {
                "tools": [
                    {
                        "kind": "database",
                        "id": "save_record",
                        "displayName": "Save Record",
                        "description": "Write one record.",
                        "operation": "write",
                        "scope": "write",
                        "idempotencyArgument": "request_key",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"request_key": {"type": "integer"}},
                            "required": [],
                        },
                    }
                ]
            }
        }
    )

    assert _codes(missing) >= {
        "semantic.database_idempotency_required",
        "semantic.database_scope_mismatch",
    }
    assert "semantic.database_idempotency_schema_invalid" in _codes(malformed)
