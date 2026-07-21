"""Immutable compiler intermediate representation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from linch_studio.spec.models import AgentCallNodeSpec, Blueprint, DirectToolNodeSpec

from .serializers import canonical_json


@dataclass(frozen=True, slots=True)
class ProviderIR:
    kind: str
    model: str
    api_key_env: str | None
    base_url_env: str | None
    project_env: str | None
    location: str | None
    context_window: int | None
    fallback_models: tuple[str, ...]
    thinking_json: str


@dataclass(frozen=True, slots=True)
class ResourceIR:
    resource: str
    mode: str


@dataclass(frozen=True, slots=True)
class ToolIR:
    id: str
    kind: str
    display_name: str
    description: str
    scope: str
    parallel: bool
    retryable: bool
    timeout_ms: int | None
    input_schema_json: str
    resources: tuple[ResourceIR, ...]
    operation: str | None
    idempotency_argument: str | None


@dataclass(frozen=True, slots=True)
class SubagentIR:
    id: str
    display_name: str
    description: str
    instructions: str
    tools: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SkillIR:
    id: str
    display_name: str
    description: str
    instructions: str
    allowed_tools: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WorkflowNodeIR:
    id: str
    label: str
    prompt: str
    depends_on: tuple[str, ...]
    subagent: str | None
    tools: tuple[str, ...]
    phase: str | None
    depth: int
    declaration_index: int


@dataclass(frozen=True, slots=True)
class WorkflowIR:
    id: str
    display_name: str
    description: str
    kind: str
    nodes: tuple[WorkflowNodeIR, ...]
    output: str
    max_concurrency: int


@dataclass(frozen=True, slots=True)
class VerifierIR:
    id: str
    kind: str
    config_json: str


@dataclass(frozen=True, slots=True)
class CompletionIR:
    mode: str
    max_retries: int
    verifiers: tuple[VerifierIR, ...]


@dataclass(frozen=True, slots=True)
class RoutineIR:
    id: str
    display_name: str
    kind: str
    target: str
    triggers: tuple[str, ...]
    charter: str | None
    prompt: str | None
    max_turns: int | None
    budget_json: str
    verify: VerifierIR | None
    done_when: VerifierIR | None


@dataclass(frozen=True, slots=True)
class TriggerIR:
    id: str
    kind: str
    config_json: str


@dataclass(frozen=True, slots=True)
class CompilerIR:
    api_version: str
    project_name: str
    title: str
    description: str
    package: str
    python_constraint: str
    linch_constraint: str
    blueprint_digest: str
    blueprint_yaml: str
    canonical_blueprint_json: str
    provider: ProviderIR
    primary_mode: str
    primary_instructions: str
    primary_tools: tuple[str, ...] | None
    primary_max_turns: int | None
    primary_budget_json: str
    completion: CompletionIR
    tools: tuple[ToolIR, ...]
    subagents: tuple[SubagentIR, ...]
    skills: tuple[SkillIR, ...]
    workflows: tuple[WorkflowIR, ...]
    routines: tuple[RoutineIR, ...]
    triggers: tuple[TriggerIR, ...]
    capabilities_json: str
    selected_capabilities: tuple[str, ...]
    required_env: tuple[str, ...]

    def capabilities(self) -> dict[str, Any]:
        value = json.loads(self.capabilities_json)
        if not isinstance(value, dict):
            raise TypeError("capabilities IR must decode to an object")
        return value


def normalize_blueprint(blueprint: Blueprint, *, yaml_text: str, digest: str) -> CompilerIR:
    """Copy a validated blueprint into detached, immutable compiler primitives."""

    runtime = blueprint.spec.runtime
    provider = runtime.provider
    if provider.kind is None or provider.model is None:
        raise ValueError("provider kind and model must be validated before normalization")

    tools = tuple(
        ToolIR(
            id=item.id,
            kind=item.kind,
            display_name=item.display_name,
            description=item.description,
            scope=item.scope,
            parallel=item.parallel,
            retryable=item.retryable,
            timeout_ms=item.timeout_ms,
            input_schema_json=canonical_json(item.input_schema),
            resources=tuple(ResourceIR(r.resource, r.mode) for r in item.resources),
            operation=getattr(item, "operation", None),
            idempotency_argument=getattr(item, "idempotency_argument", None),
        )
        for item in blueprint.spec.tools
    )
    subagents = tuple(
        SubagentIR(
            id=item.id,
            display_name=item.display_name,
            description=item.description,
            instructions=item.instructions,
            tools=tuple(item.tools),
        )
        for item in blueprint.spec.subagents
    )
    skills = tuple(
        SkillIR(
            id=item.id,
            display_name=item.display_name,
            description=item.description,
            instructions=item.instructions,
            allowed_tools=tuple(item.allowed_tools),
        )
        for item in blueprint.spec.skills
    )
    workflows = tuple(_workflow_ir(item) for item in blueprint.spec.workflows)
    routines = tuple(
        RoutineIR(
            id=item.id,
            display_name=item.display_name,
            kind=item.kind,
            target=item.target,
            triggers=tuple(item.triggers),
            charter=getattr(item, "charter", None),
            prompt=getattr(item, "prompt", None),
            max_turns=item.max_turns,
            budget_json=canonical_json(item.budget.model_dump(by_alias=True, mode="json")),
            verify=_verifier_ir(item.verify) if item.verify is not None else None,
            done_when=_verifier_ir(item.done_when) if item.done_when is not None else None,
        )
        for item in blueprint.spec.routines
    )
    triggers = tuple(
        TriggerIR(
            id=item.id,
            kind=item.kind,
            config_json=canonical_json(item.model_dump(by_alias=True, mode="json")),
        )
        for item in blueprint.spec.triggers
    )
    capability_data = blueprint.spec.capabilities.model_dump(by_alias=True, mode="json")
    required_env = _required_env(blueprint)
    canonical_data = blueprint.model_dump(by_alias=True, mode="json", exclude_none=True)
    primary = runtime.agent
    completion = CompletionIR(
        mode=primary.completion.mode,
        max_retries=primary.completion.max_retries,
        verifiers=tuple(_verifier_ir(item) for item in primary.completion.verifiers),
    )
    return CompilerIR(
        api_version=blueprint.api_version,
        project_name=blueprint.metadata.name,
        title=blueprint.metadata.title,
        description=blueprint.metadata.description,
        package=blueprint.spec.package,
        python_constraint=blueprint.spec.target.python,
        linch_constraint=blueprint.spec.target.linch,
        blueprint_digest=digest,
        blueprint_yaml=yaml_text,
        canonical_blueprint_json=canonical_json(canonical_data),
        provider=ProviderIR(
            kind=provider.kind,
            model=provider.model,
            api_key_env=provider.api_key_env,
            base_url_env=provider.base_url_env,
            project_env=provider.project_env,
            location=provider.location,
            context_window=provider.context_window,
            fallback_models=tuple(provider.fallback_models),
            thinking_json=canonical_json(provider.thinking.model_dump(by_alias=True, mode="json")),
        ),
        primary_mode=primary.preset,
        primary_instructions=primary.instructions,
        primary_tools=tuple(primary.tools) if primary.tools is not None else None,
        primary_max_turns=primary.max_turns,
        primary_budget_json=canonical_json(primary.budget.model_dump(by_alias=True, mode="json")),
        completion=completion,
        tools=tools,
        subagents=subagents,
        skills=skills,
        workflows=workflows,
        routines=routines,
        triggers=triggers,
        capabilities_json=canonical_json(capability_data),
        selected_capabilities=_selected_capabilities(blueprint),
        required_env=required_env,
    )


def _workflow_ir(workflow: Any) -> WorkflowIR:
    depths: dict[str, int] = {}
    pending = list(workflow.nodes)
    while pending:
        progressed = False
        for node in list(pending):
            if all(parent in depths for parent in node.depends_on):
                depths[node.id] = (
                    0
                    if not node.depends_on
                    else 1 + max(depths[parent] for parent in node.depends_on)
                )
                pending.remove(node)
                progressed = True
        if not progressed:
            raise ValueError(f"workflow {workflow.id!r} contains a cycle or missing dependency")

    nodes: list[WorkflowNodeIR] = []
    for index, node in enumerate(workflow.nodes):
        if isinstance(node, DirectToolNodeSpec):
            raise ValueError("direct tool workflow nodes are unsupported")
        if not isinstance(node, AgentCallNodeSpec):
            raise TypeError(f"unsupported workflow node: {type(node)!r}")
        nodes.append(
            WorkflowNodeIR(
                id=node.id,
                label=node.label,
                prompt=node.prompt,
                depends_on=tuple(node.depends_on),
                subagent=node.subagent,
                tools=tuple(node.tools),
                phase=node.phase,
                depth=depths[node.id],
                declaration_index=index,
            )
        )
    if workflow.output is None:
        raise ValueError(f"workflow {workflow.id!r} requires an output")
    return WorkflowIR(
        id=workflow.id,
        display_name=workflow.display_name,
        description=workflow.description,
        kind=workflow.kind,
        nodes=tuple(nodes),
        output=workflow.output,
        max_concurrency=workflow.max_concurrency,
    )


def _required_env(blueprint: Blueprint) -> tuple[str, ...]:
    values: set[str] = set()
    provider = blueprint.spec.runtime.provider
    for value in (provider.api_key_env, provider.base_url_env, provider.project_env):
        if value:
            values.add(value)
    caps = blueprint.spec.capabilities
    for value in (caps.memory.dsn_env, caps.external_database.dsn_env):
        if value:
            values.add(value)
    for server in caps.extensions.mcp_servers:
        token_env = getattr(server, "token_env", None)
        if token_env:
            values.add(token_env)
        for env_name in getattr(server, "env", {}).values():
            values.add(env_name)
    for trigger in blueprint.spec.triggers:
        secret = getattr(trigger, "signing_secret_env", None)
        if secret:
            values.add(secret)
    return tuple(sorted(values))


def _selected_capabilities(blueprint: Blueprint) -> tuple[str, ...]:
    spec = blueprint.spec
    caps = spec.capabilities
    provider_capability = (
        "providers.custom_adapter"
        if spec.runtime.provider.kind == "custom"
        else f"providers.{spec.runtime.provider.kind}"
    )
    session_capability = {
        "in_memory": "persistence.in_memory_session",
        "sqlite": "persistence.sqlite_session",
        "external": "persistence.external_session_store",
    }[caps.persistence.session_store]
    run_capability = {
        "in_memory": "persistence.in_memory_run",
        "sqlite": "persistence.sqlite_run",
        "external": "persistence.external_run_store",
    }[caps.persistence.run_store]
    permission_capability = {
        "interactive": "permissions.default_mode",
        "accept_edits": "permissions.accept_edits_mode",
        "trusted": "permissions.trusted_mode",
    }[caps.permissions.mode]
    selected = {
        f"execution.{spec.runtime.agent.preset}",
        provider_capability,
        session_capability,
        run_capability,
        permission_capability,
    }
    if caps.observation.logging:
        selected.add("observation.logging")
    if spec.workflows:
        selected.add("execution.directed_workflow")
    if spec.runtime.agent.completion.mode == "verifier_gated":
        selected.update({"execution.goal_verified", "completion.verifier_gated"})
        selected.update(
            f"completion.{item.kind}" for item in spec.runtime.agent.completion.verifiers
        )
    else:
        selected.add("completion.agent_judged")
    if spec.routines:
        selected.add("execution.routine")
    selected.update(
        {
            "function": "tools.function_skeleton",
            "class": "tools.class_skeleton",
            "database": "tools.database_implementation",
        }[item.kind]
        for item in spec.tools
    )
    if caps.prompt.text or caps.prompt.sections:
        selected.add(
            "prompt.replace_defaults"
            if caps.prompt.mode == "replace_defaults"
            else "prompt.append_defaults"
        )
    if caps.compaction.strategy != "none":
        selected.add(
            "compaction.general_domain"
            if caps.compaction.strategy == "general"
            else f"compaction.{caps.compaction.strategy}_strategy"
            if caps.compaction.strategy == "custom"
            else f"compaction.{caps.compaction.strategy}"
        )
    if caps.compaction.ladder.enabled:
        selected.add("compaction.ladder")
    if caps.structured_output.enabled:
        selected.add("structured_output.json_schema")
    if caps.context.memory_recall:
        selected.add("context_rag.memory_recall")
    if caps.memory.backend != "none":
        selected.add(
            "memory.custom_store"
            if caps.memory.backend == "custom"
            else f"memory.{caps.memory.backend}"
        )
    if caps.filesystem.backend != "none":
        selected.add(
            "filesystem.external_backend"
            if caps.filesystem.backend == "external"
            else f"filesystem.{caps.filesystem.backend}_backend"
        )
    if caps.observation.otel:
        selected.add("observation.otel")
    if caps.external_database.enabled:
        selected.add("tools.database_implementation")
    if caps.extensions.mcp_servers:
        selected.update(f"extensions.mcp_{server.kind}" for server in caps.extensions.mcp_servers)
    if spec.skills:
        selected.add("extensions.skill_files")
    if spec.subagents:
        selected.add("extensions.subagent_files")
    if caps.evals.enabled:
        selected.add("evals.offline_scripted_tests")
    return tuple(sorted(selected))


def _verifier_ir(verifier: Any) -> VerifierIR:
    return VerifierIR(
        id=verifier.id,
        kind=verifier.kind,
        config_json=canonical_json(verifier.model_dump(by_alias=True, mode="json")),
    )


__all__ = ["CompilerIR", "normalize_blueprint"]
