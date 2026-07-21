"""Cross-reference, safety, and capability validation for parsed blueprints."""

from __future__ import annotations

import keyword
import re
from collections import defaultdict
from collections.abc import Sequence
from typing import Any

from .diagnostics import Diagnostic, PathPart, json_path, sort_diagnostics
from .models import (
    AgentCallNodeSpec,
    AgentTickRoutineSpec,
    Blueprint,
    BudgetSpec,
    CronTriggerSpec,
    CustomTodoVerifierSpec,
    DatabaseToolSpec,
    DirectToolNodeSpec,
    ToolPermissionRuleSpec,
    WebhookTriggerSpec,
    WorkflowRunRoutineSpec,
    WorkflowSpec,
)

# Names that collide with generated package modules, Python packaging layout,
# or Linch itself. IDs remain loadable drafts; semantic validation blocks export.
RESERVED_MODULE_NAMES = frozenset(
    {
        "agent",
        "aux",
        "com1",
        "com2",
        "com3",
        "com4",
        "com5",
        "com6",
        "com7",
        "com8",
        "com9",
        "con",
        "evals",
        "integrations",
        "linch",
        "linch_studio",
        "lpt1",
        "lpt2",
        "lpt3",
        "lpt4",
        "lpt5",
        "lpt6",
        "lpt7",
        "lpt8",
        "lpt9",
        "loops",
        "observability",
        "nul",
        "prompts",
        "prn",
        "providers",
        "reliability",
        "resources",
        "routines",
        "settings",
        "src",
        "test",
        "tests",
        "tools",
        "workflows",
    }
)


def _finding(
    code: str,
    path: Sequence[PathPart],
    message: str,
    remediation: str,
    *,
    severity: str = "error",
) -> Diagnostic:
    return Diagnostic(
        code=code,
        severity=severity,
        path=json_path(*path),
        message=message,
        remediation=remediation,
    )


def _bounded(budget: BudgetSpec) -> bool:
    return budget.max_tokens is not None or budget.max_cost_usd is not None


def _validate_emitted_name(
    value: str,
    path: Sequence[PathPart],
    diagnostics: list[Diagnostic],
) -> None:
    if keyword.iskeyword(value):
        diagnostics.append(
            _finding(
                "semantic.python_keyword",
                path,
                "An identifier is a Python keyword.",
                "Choose a lowercase identifier that is not a Python keyword.",
            )
        )
    if value.casefold() in RESERVED_MODULE_NAMES:
        diagnostics.append(
            _finding(
                "semantic.reserved_module_name",
                path,
                "An identifier is reserved by the generated project layout.",
                "Choose an identifier that does not collide with a generated module.",
            )
        )


def _validate_id_collection(
    items: Sequence[Any],
    base_path: Sequence[PathPart],
    diagnostics: list[Diagnostic],
) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for index, item in enumerate(items):
        identifier = item.id
        path = (*base_path, index, "id")
        _validate_emitted_name(identifier, path, diagnostics)
        if identifier in seen:
            diagnostics.append(
                _finding(
                    "semantic.duplicate_id",
                    path,
                    "An ID is duplicated in this collection.",
                    "Assign a unique ID within the collection.",
                )
            )
        else:
            seen[identifier] = item
    return seen


def _validate_global_component_ids(
    blueprint: Blueprint,
    diagnostics: list[Diagnostic],
) -> None:
    """Reject IDs that would collide in generated ``docs/components`` paths."""

    components: list[tuple[str, Sequence[Any], tuple[PathPart, ...]]] = [
        ("tool", blueprint.spec.tools, ("spec", "tools")),
        ("subagent", blueprint.spec.subagents, ("spec", "subagents")),
        ("skill", blueprint.spec.skills, ("spec", "skills")),
        ("workflow", blueprint.spec.workflows, ("spec", "workflows")),
        ("routine", blueprint.spec.routines, ("spec", "routines")),
        (
            "verifier",
            blueprint.spec.runtime.agent.completion.verifiers,
            ("spec", "runtime", "agent", "completion", "verifiers"),
        ),
    ]
    seen: dict[str, tuple[str, tuple[PathPart, ...]]] = {}
    for component_kind, items, base in components:
        for index, item in enumerate(items):
            path = (*base, index, "id")
            previous = seen.get(item.id)
            if previous is None:
                seen[item.id] = (component_kind, path)
                continue
            previous_kind, _ = previous
            if previous_kind == component_kind:
                # Collection-local duplicate diagnostics are more precise.
                continue
            diagnostics.append(
                _finding(
                    "semantic.component_id_collision",
                    path,
                    f"This {component_kind} ID collides with an existing {previous_kind} ID.",
                    "Use one globally unique ID for every generated component and verifier.",
                )
            )

    for routine_index, routine in enumerate(blueprint.spec.routines):
        for field_name, verifier in (("verify", routine.verify), ("doneWhen", routine.done_when)):
            if verifier is None:
                continue
            path = ("spec", "routines", routine_index, field_name, "id")
            previous = seen.get(verifier.id)
            if previous is None:
                seen[verifier.id] = ("verifier", path)
                continue
            previous_kind, previous_path = previous
            if previous_kind == "verifier" and previous_path[:-2] == path[:-2]:
                # A routine's verify/doneWhen duplicate has a dedicated diagnostic.
                continue
            diagnostics.append(
                _finding(
                    "semantic.component_id_collision",
                    path,
                    f"This verifier ID collides with an existing {previous_kind} ID.",
                    "Use one globally unique ID for every generated component and verifier.",
                )
            )


def _missing_reference(
    diagnostics: list[Diagnostic],
    path: Sequence[PathPart],
    *,
    target: str,
) -> None:
    diagnostics.append(
        _finding(
            "semantic.missing_reference",
            path,
            "A referenced component does not exist.",
            f"Select an existing {target} ID or create that component first.",
        )
    )


def _outside_runtime_tool_pool(
    diagnostics: list[Diagnostic],
    path: Sequence[PathPart],
) -> None:
    diagnostics.append(
        _finding(
            "semantic.tool_outside_runtime_pool",
            path,
            "The tool is excluded from the primary runtime registry.",
            (
                "Add it to runtime.agent.tools; child, workflow, and skill filters can only "
                "narrow the primary registry."
            ),
        )
    )


def _validate_provider(blueprint: Blueprint, diagnostics: list[Diagnostic]) -> None:
    provider = blueprint.spec.runtime.provider
    path = ("spec", "runtime", "provider")
    if provider.kind is None:
        diagnostics.append(
            _finding(
                "semantic.provider_required",
                (*path, "kind"),
                "A provider must be selected before export.",
                "Select one provider from the supported provider catalog.",
            )
        )
    if provider.model is None:
        diagnostics.append(
            _finding(
                "semantic.provider_model_required",
                (*path, "model"),
                "A provider model must be selected before export.",
                "Set the model identifier for the selected provider.",
            )
        )
    if provider.kind == "custom":
        diagnostics.append(
            _finding(
                "semantic.skeleton_todo",
                (*path, "kind"),
                "The custom provider is generated as a TODO adapter.",
                "Implement and contract-test the generated provider before production use.",
                severity="warning",
            )
        )
    if provider.kind != "gemini":
        if provider.project_env is not None:
            diagnostics.append(
                _finding(
                    "semantic.provider_option_incompatible",
                    (*path, "projectEnv"),
                    "This provider option is only supported by Gemini.",
                    "Remove the option or select the Gemini provider.",
                )
            )
        if provider.location is not None:
            diagnostics.append(
                _finding(
                    "semantic.provider_option_incompatible",
                    (*path, "location"),
                    "This provider option is only supported by Gemini.",
                    "Remove the option or select the Gemini provider.",
                )
            )
    adjustable_context = {"openai_chat", "llama_cpp", "vllm", "sglang", "custom"}
    if provider.context_window is not None and provider.kind not in adjustable_context:
        diagnostics.append(
            _finding(
                "semantic.provider_option_incompatible",
                (*path, "contextWindow"),
                "The selected provider does not expose a context-window override.",
                "Remove the override or select a compatible provider.",
            )
        )
    if provider.thinking.budget_tokens is not None and provider.kind not in {
        "anthropic",
        "gemini",
    }:
        diagnostics.append(
            _finding(
                "semantic.provider_option_incompatible",
                (*path, "thinking", "budgetTokens"),
                "The selected provider does not support a thinking token budget.",
                "Remove the budget or select a compatible provider.",
            )
        )

    seen_models: set[str] = set()
    if provider.model is not None:
        seen_models.add(provider.model)
    for index, model in enumerate(provider.fallback_models):
        if model in seen_models:
            diagnostics.append(
                _finding(
                    "semantic.duplicate_fallback_model",
                    (*path, "fallbackModels", index),
                    "A fallback model is duplicated in the provider chain.",
                    "Keep each primary or fallback model only once.",
                )
            )
        seen_models.add(model)


def _validate_limits(blueprint: Blueprint, diagnostics: list[Diagnostic]) -> None:
    agent = blueprint.spec.runtime.agent
    base = ("spec", "runtime", "agent")
    if agent.preset in {"deep_agent", "coordinator"}:
        if agent.max_turns is None:
            diagnostics.append(
                _finding(
                    "semantic.autonomous_max_turns_required",
                    (*base, "maxTurns"),
                    "Autonomous agents require an explicit turn limit.",
                    "Set maxTurns to a positive, workload-appropriate bound.",
                )
            )
        if not _bounded(agent.budget):
            diagnostics.append(
                _finding(
                    "semantic.autonomous_budget_required",
                    (*base, "budget"),
                    "Autonomous agents require an explicit token or cost budget.",
                    "Set maxTokens, maxCostUsd, or both.",
                )
            )
    if agent.preset == "coordinator" and not blueprint.spec.subagents:
        diagnostics.append(
            _finding(
                "semantic.coordinator_worker_required",
                ("spec", "subagents"),
                "Coordinator mode requires at least one worker definition.",
                "Add a subagent worker or select another agent preset.",
            )
        )

    completion = agent.completion
    completion_path = (*base, "completion")
    if completion.mode == "agent_judged" and completion.verifiers:
        diagnostics.append(
            _finding(
                "semantic.agent_judged_verifiers_forbidden",
                (*completion_path, "verifiers"),
                "Agent-judged completion cannot install final-answer verifiers.",
                "Remove the verifiers or select verifier_gated completion.",
            )
        )
    if completion.mode == "verifier_gated":
        if not completion.verifiers:
            diagnostics.append(
                _finding(
                    "semantic.completion_verifier_required",
                    (*completion_path, "verifiers"),
                    "Verifier-gated completion requires at least one verifier.",
                    "Add a deterministic verifier or select agent_judged completion.",
                )
            )
        if agent.max_turns is None and not _bounded(agent.budget):
            diagnostics.append(
                _finding(
                    "semantic.completion_limit_required",
                    completion_path,
                    "Verifier-gated completion requires a turn, token, or cost limit.",
                    "Set runtime.agent.maxTurns or a bounded runtime.agent.budget.",
                )
            )
    for index, verifier in enumerate(completion.verifiers):
        _validate_emitted_name(
            verifier.id,
            (*completion_path, "verifiers", index, "id"),
            diagnostics,
        )
        if isinstance(verifier, CustomTodoVerifierSpec):
            _skeleton_warning(
                diagnostics,
                (*completion_path, "verifiers", index),
                message="The custom completion verifier is generated as a blocking TODO.",
            )


def _validate_tools_and_references(
    blueprint: Blueprint,
    diagnostics: list[Diagnostic],
    tool_ids: set[str],
    subagent_ids: set[str],
) -> None:
    primary_tools = blueprint.spec.runtime.agent.tools
    primary_tool_pool = set(primary_tools) if primary_tools is not None else None
    if primary_tools is not None:
        seen_primary_tools: set[str] = set()
        for ref_index, tool_id in enumerate(primary_tools):
            path = ("spec", "runtime", "agent", "tools", ref_index)
            if tool_id in seen_primary_tools:
                diagnostics.append(
                    _finding(
                        "semantic.duplicate_reference",
                        path,
                        "A runtime-agent tool reference is repeated.",
                        "Keep each tool at most once in the runtime-agent allow-list.",
                    )
                )
            else:
                seen_primary_tools.add(tool_id)
            if tool_id not in tool_ids:
                _missing_reference(diagnostics, path, target="tool")

    for index, tool in enumerate(blueprint.spec.tools):
        if isinstance(tool, DatabaseToolSpec):
            if tool.operation == "write" and tool.idempotency_argument is None:
                diagnostics.append(
                    _finding(
                        "semantic.database_idempotency_required",
                        ("spec", "tools", index, "idempotencyArgument"),
                        "Database write tools require an idempotency-key argument.",
                        "Add a required input argument used to deduplicate writes.",
                    )
                )
            if tool.idempotency_argument is not None:
                _validate_emitted_name(
                    tool.idempotency_argument,
                    ("spec", "tools", index, "idempotencyArgument"),
                    diagnostics,
                )
                if tool.operation == "write":
                    properties = tool.input_schema.get("properties")
                    required = tool.input_schema.get("required")
                    declaration = (
                        properties.get(tool.idempotency_argument)
                        if isinstance(properties, dict)
                        else None
                    )
                    if (
                        not isinstance(declaration, dict)
                        or declaration.get("type") != "string"
                        or not isinstance(required, list)
                        or tool.idempotency_argument not in required
                    ):
                        diagnostics.append(
                            _finding(
                                "semantic.database_idempotency_schema_invalid",
                                ("spec", "tools", index, "inputSchema"),
                                "The idempotency argument must be a required string input.",
                                "Declare the named property with type string and include it in "
                                "inputSchema.required.",
                            )
                        )
            if tool.operation == "write" and tool.scope != "write":
                diagnostics.append(
                    _finding(
                        "semantic.database_scope_mismatch",
                        ("spec", "tools", index, "scope"),
                        "A database write operation must use write scope.",
                        "Set scope to write so scheduling and permissions remain safe.",
                    )
                )

    for index, subagent in enumerate(blueprint.spec.subagents):
        for ref_index, tool_id in enumerate(subagent.tools):
            path = ("spec", "subagents", index, "tools", ref_index)
            if tool_id not in tool_ids:
                _missing_reference(diagnostics, path, target="tool")
            elif primary_tool_pool is not None and tool_id not in primary_tool_pool:
                _outside_runtime_tool_pool(diagnostics, path)
    for index, skill in enumerate(blueprint.spec.skills):
        for ref_index, tool_id in enumerate(skill.allowed_tools):
            path = ("spec", "skills", index, "allowedTools", ref_index)
            if tool_id not in tool_ids:
                _missing_reference(diagnostics, path, target="tool")
            elif primary_tool_pool is not None and tool_id not in primary_tool_pool:
                _outside_runtime_tool_pool(diagnostics, path)
    for workflow_index, workflow in enumerate(blueprint.spec.workflows):
        for node_index, node in enumerate(workflow.nodes):
            if isinstance(node, AgentCallNodeSpec):
                if node.subagent is not None and node.subagent not in subagent_ids:
                    _missing_reference(
                        diagnostics,
                        ("spec", "workflows", workflow_index, "nodes", node_index, "subagent"),
                        target="subagent",
                    )
                for ref_index, tool_id in enumerate(node.tools):
                    path = (
                        "spec",
                        "workflows",
                        workflow_index,
                        "nodes",
                        node_index,
                        "tools",
                        ref_index,
                    )
                    if tool_id not in tool_ids:
                        _missing_reference(diagnostics, path, target="tool")
                    elif primary_tool_pool is not None and tool_id not in primary_tool_pool:
                        _outside_runtime_tool_pool(diagnostics, path)
            elif isinstance(node, DirectToolNodeSpec):
                diagnostics.append(
                    _finding(
                        "semantic.direct_tool_unsupported",
                        ("spec", "workflows", workflow_index, "nodes", node_index, "type"),
                        "Direct tool workflow nodes are unsupported in v1alpha2.",
                        "Move tool use inside an agent-call node.",
                    )
                )
                if node.tool not in tool_ids:
                    _missing_reference(
                        diagnostics,
                        ("spec", "workflows", workflow_index, "nodes", node_index, "tool"),
                        target="tool",
                    )


def _validate_workflow(
    workflow: WorkflowSpec,
    workflow_index: int,
    diagnostics: list[Diagnostic],
) -> None:
    base = ("spec", "workflows", workflow_index)
    node_by_id: dict[str, Any] = {}
    node_index: dict[str, int] = {}
    for index, node in enumerate(workflow.nodes):
        _validate_emitted_name(node.id, (*base, "nodes", index, "id"), diagnostics)
        if node.id in node_by_id:
            diagnostics.append(
                _finding(
                    "semantic.duplicate_id",
                    (*base, "nodes", index, "id"),
                    "A workflow node ID is duplicated.",
                    "Assign a unique ID within this workflow.",
                )
            )
        else:
            node_by_id[node.id] = node
            node_index[node.id] = index

    if not workflow.nodes:
        diagnostics.append(
            _finding(
                "semantic.workflow_empty",
                (*base, "nodes"),
                "A directed workflow must contain at least one node.",
                "Add one or more agent-call nodes.",
            )
        )
    if workflow.output is None:
        diagnostics.append(
            _finding(
                "semantic.workflow_output_required",
                (*base, "output"),
                "A directed workflow requires one explicit output node.",
                "Select exactly one existing terminal node as output.",
            )
        )
    elif workflow.output not in node_by_id:
        _missing_reference(diagnostics, (*base, "output"), target="workflow node")

    successors: dict[str, list[str]] = {node_id: [] for node_id in node_by_id}
    indegree: dict[str, int] = {node_id: 0 for node_id in node_by_id}
    valid_dependencies: dict[str, list[str]] = {node_id: [] for node_id in node_by_id}
    references_valid = True
    for index, node in enumerate(workflow.nodes):
        if node_by_id.get(node.id) is not node:
            continue
        seen_dependencies: set[str] = set()
        for ref_index, dependency in enumerate(node.depends_on):
            path = (*base, "nodes", index, "dependsOn", ref_index)
            if dependency in seen_dependencies:
                diagnostics.append(
                    _finding(
                        "semantic.duplicate_reference",
                        path,
                        "A workflow dependency is repeated.",
                        "Keep each predecessor only once per node.",
                    )
                )
                continue
            seen_dependencies.add(dependency)
            if dependency not in node_by_id:
                references_valid = False
                _missing_reference(diagnostics, path, target="workflow node")
                continue
            valid_dependencies[node.id].append(dependency)
            indegree[node.id] += 1
            successors[dependency].append(node.id)

    if workflow.output in successors and successors[workflow.output]:
        diagnostics.append(
            _finding(
                "semantic.workflow_output_not_terminal",
                (*base, "output"),
                "The workflow output node must be terminal.",
                "Choose a node that has no downstream dependents.",
            )
        )

    if not references_valid or len(node_by_id) != len(workflow.nodes):
        return

    declaration_order = [node.id for node in workflow.nodes]
    ready = [node_id for node_id in declaration_order if indegree[node_id] == 0]
    depths: dict[str, int] = {node_id: 0 for node_id in ready}
    processed: list[str] = []
    while ready:
        current = ready.pop(0)
        processed.append(current)
        for successor in successors[current]:
            depths[successor] = max(depths.get(successor, 0), depths[current] + 1)
            indegree[successor] -= 1
            if indegree[successor] == 0:
                ready.append(successor)

    if len(processed) != len(node_by_id):
        diagnostics.append(
            _finding(
                "semantic.workflow_cycle",
                (*base, "nodes"),
                "The directed workflow contains a dependency cycle.",
                "Remove cyclic dependencies so the graph is acyclic.",
            )
        )
        return

    phases_by_depth: dict[int, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    for node_id in declaration_order:
        node = node_by_id[node_id]
        if isinstance(node, AgentCallNodeSpec) and node.phase is not None:
            phases_by_depth[depths[node_id]][node.phase].append(node_index[node_id])
    for phase_groups in phases_by_depth.values():
        if len(phase_groups) <= 1:
            continue
        for indices in phase_groups.values():
            for index in indices:
                diagnostics.append(
                    _finding(
                        "semantic.workflow_phase_conflict",
                        (*base, "nodes", index, "phase"),
                        "Nodes at the same topological depth use different phase labels.",
                        "Use one phase label per depth or leave phase unset.",
                    )
                )


def _validate_routines_and_triggers(
    blueprint: Blueprint,
    diagnostics: list[Diagnostic],
    workflows: set[str],
    triggers: dict[str, Any],
) -> None:
    for index, trigger in enumerate(blueprint.spec.triggers):
        if isinstance(trigger, CronTriggerSpec):
            try:
                from linch import validate_cron

                validate_cron(trigger.cron)
            except (ImportError, ValueError):
                diagnostics.append(
                    _finding(
                        "semantic.invalid_cron",
                        ("spec", "triggers", index, "cron"),
                        "The cron expression is not a valid five-field schedule.",
                        "Use minute, hour, day-of-month, month, and day-of-week fields.",
                    )
                )
        if isinstance(trigger, WebhookTriggerSpec) and trigger.signing_secret_env is None:
            diagnostics.append(
                _finding(
                    "semantic.webhook_signing_secret_required",
                    ("spec", "triggers", index, "signingSecretEnv"),
                    "Webhook triggers require an environment reference for signature checking.",
                    "Set signingSecretEnv and implement the generated blocking verifier TODO.",
                )
            )

    agent = blueprint.spec.runtime.agent
    for index, routine in enumerate(blueprint.spec.routines):
        base = ("spec", "routines", index)
        if isinstance(routine, WorkflowRunRoutineSpec) and routine.target not in workflows:
            _missing_reference(
                diagnostics,
                (*base, "target"),
                target="directed workflow",
            )
        elif isinstance(routine, AgentTickRoutineSpec) and routine.target != "runtime_agent":
            # The literal model normally catches this; retain an authoritative semantic check for
            # programmatic model construction and future target expansion.
            diagnostics.append(
                _finding(
                    "semantic.routine_target_invalid",
                    (*base, "target"),
                    "An agent_tick routine must target runtime_agent.",
                    "Set target to runtime_agent or select workflow_run.",
                )
            )
        for ref_index, trigger_id in enumerate(routine.triggers):
            if trigger_id not in triggers:
                _missing_reference(
                    diagnostics,
                    (*base, "triggers", ref_index),
                    target="trigger",
                )
        selected_verifiers = (
            ("verify", routine.verify),
            ("doneWhen", routine.done_when),
        )
        seen_verifier_ids: set[str] = set()
        for field_name, verifier in selected_verifiers:
            if verifier is None:
                continue
            _validate_emitted_name(verifier.id, (*base, field_name, "id"), diagnostics)
            if verifier.id in seen_verifier_ids:
                diagnostics.append(
                    _finding(
                        "semantic.duplicate_id",
                        (*base, field_name, "id"),
                        "A routine verifier ID is duplicated.",
                        "Use distinct IDs for verify and doneWhen.",
                    )
                )
            seen_verifier_ids.add(verifier.id)
            if isinstance(verifier, CustomTodoVerifierSpec):
                _skeleton_warning(
                    diagnostics,
                    (*base, field_name),
                    message="The custom routine verifier is generated as a blocking TODO.",
                )

    permissions = blueprint.spec.capabilities.permissions
    if permissions.mode == "trusted":
        diagnostics.append(
            _finding(
                "semantic.trusted_permissions",
                ("spec", "capabilities", "permissions", "mode"),
                "Trusted mode allows every otherwise-unmatched tool call.",
                "Use ordered rules and a narrower mode whenever possible.",
                severity="warning",
            )
        )

    def explicitly_resolved(tool_id: str) -> bool:
        for rule in permissions.rules:
            if not isinstance(rule, ToolPermissionRuleSpec):
                continue
            if rule.tool not in {"*", tool_id} or rule.argument is not None:
                continue
            if rule.decision == "passthrough":
                continue
            return rule.decision in {"allow", "deny"}
        return False

    unresolved_non_read = [
        tool
        for tool in blueprint.spec.tools
        if tool.scope != "read" and not explicitly_resolved(tool.id)
    ]
    unresolved_exec = [tool for tool in unresolved_non_read if tool.scope == "exec"]
    model_directed_unresolved = agent.preset in {
        "deep_agent",
        "coordinator",
    } and not explicitly_resolved("*")
    for routine_index, routine in enumerate(blueprint.spec.routines):
        headless = any(
            trigger_id in triggers and triggers[trigger_id].kind in {"cron", "ci", "webhook"}
            for trigger_id in routine.triggers
        )
        if not headless:
            continue
        has_invocation_limit = (
            routine.max_turns is not None
            or _bounded(routine.budget)
            or agent.max_turns is not None
            or _bounded(agent.budget)
        )
        if not has_invocation_limit:
            diagnostics.append(
                _finding(
                    "semantic.headless_limit_required",
                    ("spec", "routines", routine_index),
                    "A headless routine requires a turn, token, or cost limit.",
                    "Set a routine or runtime maxTurns, maxTokens, or maxCostUsd limit.",
                )
            )
        if permissions.mode == "interactive" and (unresolved_non_read or model_directed_unresolved):
            diagnostics.append(
                _finding(
                    "semantic.headless_permissions",
                    ("spec", "routines", routine_index, "triggers"),
                    "A headless routine may pause indefinitely for tool approval.",
                    "Use read-only tools or configure a non-interactive permission policy.",
                )
            )
        elif permissions.mode == "accept_edits" and (unresolved_exec or model_directed_unresolved):
            diagnostics.append(
                _finding(
                    "semantic.headless_permissions",
                    ("spec", "routines", routine_index, "triggers"),
                    "A headless routine may pause indefinitely for exec-tool approval.",
                    "Deny exec tools, add unconditional allow rules, or use trusted mode "
                    "explicitly.",
                )
            )


def _validate_persistence(blueprint: Blueprint, diagnostics: list[Diagnostic]) -> None:
    persistence = blueprint.spec.capabilities.persistence
    base = ("spec", "capabilities", "persistence")
    if persistence.durable_resume and (
        persistence.session_store == "in_memory" or persistence.run_store == "in_memory"
    ):
        diagnostics.append(
            _finding(
                "semantic.persistence_claim_invalid",
                (*base, "durableResume"),
                "Durable resume cannot use in-memory session or run stores.",
                "Select SQLite or explicit external adapters for both stores.",
            )
        )
    for field_name, value in (
        ("sessionStore", persistence.session_store),
        ("runStore", persistence.run_store),
    ):
        if value == "external":
            diagnostics.append(
                _finding(
                    "semantic.skeleton_todo",
                    (*base, field_name),
                    "The external persistence adapter is generated as a TODO.",
                    "Implement and contract-test the adapter before claiming durability.",
                    severity="warning",
                )
            )


def _skeleton_warning(
    diagnostics: list[Diagnostic],
    path: Sequence[PathPart],
    *,
    message: str,
) -> None:
    diagnostics.append(
        _finding(
            "semantic.skeleton_todo",
            path,
            message,
            "Complete the generated TODO and its contract tests before production use.",
            severity="warning",
        )
    )


def _validate_capabilities(blueprint: Blueprint, diagnostics: list[Diagnostic]) -> None:
    capabilities = blueprint.spec.capabilities
    base = ("spec", "capabilities")

    skeletons: list[tuple[bool, tuple[PathPart, ...], str]] = [
        (
            capabilities.extensions.live_mcp_discovery,
            (*base, "extensions", "liveMcpDiscovery"),
            "Live MCP discovery is generated as a TODO.",
        ),
        (
            capabilities.prompt.custom_dynamic_policy,
            (*base, "prompt", "customDynamicPolicy"),
            "The dynamic prompt policy is generated as a TODO.",
        ),
        (
            capabilities.reliability.custom_token_estimator,
            (*base, "reliability", "customTokenEstimator"),
            "The custom token estimator is generated as a TODO.",
        ),
        (
            capabilities.reliability.custom_recovery_policy,
            (*base, "reliability", "customRecoveryPolicy"),
            "The custom recovery policy is generated as a TODO.",
        ),
        (
            capabilities.compaction.strategy == "custom",
            (*base, "compaction", "strategy"),
            "The custom compaction strategy is generated as a TODO.",
        ),
        (
            capabilities.context.custom_builder,
            (*base, "context", "customBuilder"),
            "The custom context builder is generated as a TODO.",
        ),
        (
            capabilities.context.dynamic_tool_selector,
            (*base, "context", "dynamicToolSelector"),
            "The dynamic tool selector is generated as a TODO.",
        ),
        (
            capabilities.memory.backend in {"faiss", "pgvector", "qdrant", "custom"},
            (*base, "memory", "backend"),
            "The selected memory backend is generated as an adapter TODO.",
        ),
        (
            capabilities.filesystem.backend == "external",
            (*base, "filesystem", "backend"),
            "The external file backend is generated as a TODO.",
        ),
        (
            capabilities.hooks.custom_middleware,
            (*base, "hooks", "customMiddleware"),
            "The custom middleware is generated as a TODO.",
        ),
        (
            capabilities.observation.vendor_exporter is not None,
            (*base, "observation", "vendorExporter"),
            "The vendor-specific exporter is generated as a TODO.",
        ),
        (
            capabilities.permissions.hitl_callback_skeleton,
            (*base, "permissions", "hitlCallbackSkeleton"),
            "The HITL callback is generated as a TODO.",
        ),
        (
            capabilities.permissions.organization_policy_skeleton,
            (*base, "permissions", "organizationPolicySkeleton"),
            "The organization policy is generated as a TODO.",
        ),
        (
            capabilities.extensions.mailbox_adapter == "external",
            (*base, "extensions", "mailboxAdapter"),
            "The external mailbox adapter is generated as a TODO.",
        ),
        (
            capabilities.extensions.schedule_store_adapter == "external",
            (*base, "extensions", "scheduleStoreAdapter"),
            "The external schedule-store adapter is generated as a TODO.",
        ),
        (
            capabilities.extensions.isolation_adapter == "external",
            (*base, "extensions", "isolationAdapter"),
            "The external isolation adapter is generated as a TODO.",
        ),
        (
            capabilities.evals.domain_scorer_skeleton,
            (*base, "evals", "domainScorerSkeleton"),
            "The domain scorer is generated as a TODO.",
        ),
        (
            capabilities.external_database.enabled,
            (*base, "externalDatabase", "enabled"),
            "External database integration contains implementation TODOs.",
        ),
    ]
    for enabled, path, message in skeletons:
        if enabled:
            _skeleton_warning(diagnostics, path, message=message)

    memory = capabilities.memory
    if (capabilities.context.memory_recall or memory.search_tool or memory.upsert_tool) and (
        memory.backend == "none"
    ):
        diagnostics.append(
            _finding(
                "semantic.memory_backend_required",
                (*base, "memory", "backend"),
                "Memory features require a configured memory backend.",
                "Select a memory backend or disable memory-dependent features.",
            )
        )
    if memory.backend == "postgres_keyword" and memory.dsn_env is None:
        diagnostics.append(
            _finding(
                "semantic.memory_dsn_required",
                (*base, "memory", "dsnEnv"),
                "Postgres keyword memory requires a DSN environment reference.",
                "Set dsnEnv to an environment-variable name.",
            )
        )

    structured = capabilities.structured_output
    if not structured.enabled and (
        structured.final_tool_name is not None or structured.repair_retries > 0
    ):
        diagnostics.append(
            _finding(
                "semantic.structured_output_disabled",
                (*base, "structuredOutput", "enabled"),
                "Structured-output options are set while structured output is disabled.",
                "Enable structured output or remove its final-tool and repair settings.",
            )
        )
    if structured.final_tool_name is not None:
        _validate_emitted_name(
            structured.final_tool_name,
            (*base, "structuredOutput", "finalToolName"),
            diagnostics,
        )

    external_db = capabilities.external_database
    if external_db.enabled and external_db.dsn_env is None:
        diagnostics.append(
            _finding(
                "semantic.external_database_dsn_required",
                (*base, "externalDatabase", "dsnEnv"),
                "External database integration requires a DSN environment reference.",
                "Set dsnEnv to an environment-variable name.",
            )
        )
    if not external_db.enabled and (external_db.adapter_templates or external_db.vector_guidance):
        diagnostics.append(
            _finding(
                "semantic.external_database_disabled",
                (*base, "externalDatabase", "enabled"),
                "External database templates are selected while the pack is disabled.",
                "Enable the external database pack or clear its templates and guidance.",
            )
        )

    for index, rule in enumerate(capabilities.hooks.redaction_rules):
        try:
            re.compile(rule.pattern)
        except re.error:
            diagnostics.append(
                _finding(
                    "semantic.invalid_redaction_regex",
                    (*base, "hooks", "redactionRules", index, "pattern"),
                    "A redaction rule is not a valid regular expression.",
                    "Replace it with a valid bounded regular expression.",
                )
            )


def validate_blueprint(blueprint: Blueprint) -> tuple[Diagnostic, ...]:
    """Return deterministic semantic findings without rejecting the model."""

    diagnostics: list[Diagnostic] = []
    _validate_emitted_name(blueprint.metadata.name, ("metadata", "name"), diagnostics)
    _validate_emitted_name(blueprint.spec.package, ("spec", "package"), diagnostics)

    tool_map = _validate_id_collection(blueprint.spec.tools, ("spec", "tools"), diagnostics)
    subagent_map = _validate_id_collection(
        blueprint.spec.subagents, ("spec", "subagents"), diagnostics
    )
    _validate_id_collection(blueprint.spec.skills, ("spec", "skills"), diagnostics)
    workflow_map = _validate_id_collection(
        blueprint.spec.workflows, ("spec", "workflows"), diagnostics
    )
    _validate_id_collection(blueprint.spec.routines, ("spec", "routines"), diagnostics)
    trigger_map = _validate_id_collection(
        blueprint.spec.triggers, ("spec", "triggers"), diagnostics
    )
    _validate_id_collection(
        blueprint.spec.capabilities.prompt.sections,
        ("spec", "capabilities", "prompt", "sections"),
        diagnostics,
    )
    _validate_id_collection(
        blueprint.spec.capabilities.extensions.mcp_servers,
        ("spec", "capabilities", "extensions", "mcpServers"),
        diagnostics,
    )
    _validate_id_collection(
        blueprint.spec.capabilities.evals.cases,
        ("spec", "capabilities", "evals", "cases"),
        diagnostics,
    )
    _validate_id_collection(
        blueprint.spec.runtime.agent.completion.verifiers,
        ("spec", "runtime", "agent", "completion", "verifiers"),
        diagnostics,
    )
    _validate_global_component_ids(blueprint, diagnostics)

    _validate_provider(blueprint, diagnostics)
    _validate_limits(blueprint, diagnostics)
    _validate_tools_and_references(
        blueprint,
        diagnostics,
        set(tool_map),
        set(subagent_map),
    )
    for index, workflow in enumerate(blueprint.spec.workflows):
        _validate_workflow(workflow, index, diagnostics)
    _validate_routines_and_triggers(
        blueprint,
        diagnostics,
        set(workflow_map),
        trigger_map,
    )
    _validate_persistence(blueprint, diagnostics)
    _validate_capabilities(blueprint, diagnostics)
    return sort_diagnostics(diagnostics)


semantic_diagnostics = validate_blueprint


__all__ = [
    "RESERVED_MODULE_NAMES",
    "semantic_diagnostics",
    "validate_blueprint",
]
