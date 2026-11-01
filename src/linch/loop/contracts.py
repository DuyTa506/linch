"""Resume-safe run-contract construction for the loop.

This module contains only pure contract identity helpers; it deliberately has
no dependency on turn orchestration.
"""

from __future__ import annotations

import json
from typing import Any, cast

from .._version import get_version
from ..compaction import _builtin_compaction_config, _skill_listing_fingerprint
from ..errors import ConfigError
from ..model_calls import _accounting_contract
from ..permissions import Principal
from ..reliability import canonical_reliability_policy
from ..run_store import MODEL_INPUT_CODEC_VERSION, build_run_contract
from ..session import RunOptions, Session


def _exact_model_input_enabled(agent: Any) -> bool:
    return bool(getattr(getattr(agent, "durability", None), "exact_model_input", False))


def _effective_final_tool_name(agent: Any, opts: RunOptions, *, config: Any = None) -> str | None:
    """Resolve the terminal structured-output tool for this run."""
    explicit = opts.final_tool_name or getattr(agent, "final_tool_name", None)
    if explicit is not None:
        return explicit
    schema = opts.output_schema or (config or agent).output_schema
    if schema is None or not hasattr(agent.provider, "capabilities"):
        return None
    caps = agent.provider.capabilities(agent.model)
    return schema.name if getattr(caps, "structured_output_terminal_tool", False) else None


def _contract_registry(session: Session, config: Any = None) -> Any:
    """Return the intended baseline tool surface for a durable run.

    A pending skill overlay is already known at run creation and therefore
    participates in identity.  Per-turn context selection is ephemeral and is
    captured in the checkpointed request phase rather than by eagerly running
    context hooks here.
    """
    registry = (
        config.tools if config is not None else (session.tools_override or session.agent.tools)
    )
    pending = getattr(session, "pending_skill_overlay", None)
    allowed = getattr(pending, "allowed_tools", None)
    if allowed is not None:
        return registry.select(names={str(name) for name in allowed})
    return registry


def _permission_rule_contract(rule: Any) -> dict[str, Any]:
    kind = str(getattr(rule, "kind", rule.__class__.__name__))
    out: dict[str, Any] = {"kind": kind, "decision": getattr(rule, "decision", None)}
    for name in ("tool", "arg", "tools", "paths", "pattern"):
        if hasattr(rule, name):
            out[name] = getattr(rule, name)
    for name in ("role", "unless_role"):
        value = getattr(rule, name, None)
        if value is not None:
            out[name] = value
    return out


def _principal_contract(principal: Principal | None) -> dict[str, Any] | None:
    if principal is None:
        return None
    if not isinstance(principal, Principal):
        raise ConfigError("run principal must be a Principal")
    return {
        "tenant": principal.tenant,
        "user": principal.user,
        "roles": sorted(principal.roles),
    }


def _principal_from_value(raw: Any) -> Principal:
    """Decode a JSON-safe principal value."""
    if not isinstance(raw, dict) or set(raw) != {"tenant", "user", "roles"}:
        raise ConfigError("stored run contract has an invalid principal shape")
    roles = raw["roles"]
    if not isinstance(roles, list) or not all(isinstance(role, str) for role in roles):
        raise ConfigError("stored run contract has an invalid principal shape")
    try:
        return Principal(tenant=raw["tenant"], user=raw["user"], roles=frozenset(roles))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"stored run contract has an invalid principal: {exc}") from None


def _principal_from_contract(contract: Any) -> Principal | None:
    payload = getattr(contract, "payload", None)
    if not isinstance(payload, dict):
        raise ConfigError("stored run contract has an invalid principal shape")
    options = payload.get("run_options", {})
    if not isinstance(options, dict):
        raise ConfigError("stored run contract has an invalid principal shape")
    if "principal" not in options:
        return None
    return _principal_from_value(options["principal"])


def _permission_policy_contract(agent: Any) -> dict[str, Any]:
    engine = agent.permission_engine
    layered: list[dict[str, Any]] = []
    rule_set = getattr(engine, "rule_set", None)
    for layer in getattr(rule_set, "layers", ()) or ():
        layered.append(
            {
                "name": getattr(layer, "name", None),
                "rules": [_permission_rule_contract(rule) for rule in layer.rules],
            }
        )
    callback = getattr(engine, "can_use_tool", None)
    callback_identity = None
    if callback is not None:
        policy_id = getattr(callback, "resume_policy_id", getattr(callback, "policy_id", None))
        policy_version = getattr(
            callback,
            "resume_policy_version",
            getattr(callback, "policy_version", None),
        )
        if not isinstance(policy_id, str) or not policy_id:
            raise ConfigError(
                "durable runs with can_use_tool require callback.resume_policy_id "
                "(and optionally resume_policy_version)"
            )
        callback_identity = {
            "type": f"{callback.__class__.__module__}.{callback.__class__.__qualname__}",
            "name": getattr(callback, "__qualname__", getattr(callback, "__name__", None)),
            "policy_id": policy_id,
            "policy_version": policy_version,
            "config": getattr(callback, "resume_policy_config", None),
        }
    return {
        "mode": engine.mode,
        "project_root": engine.project_root,
        "rules": [_permission_rule_contract(rule) for rule in engine.rules],
        "layers": layered,
        "layer_project_root": getattr(rule_set, "project_root", None),
        "callback": callback_identity,
        "policy_id": getattr(engine, "policy_id", None),
        "policy_version": getattr(engine, "policy_version", None),
    }


def _hook_policy_contract(agent: Any) -> list[dict[str, Any]]:
    identities: list[dict[str, Any]] = []
    adapter_policies = {
        "ContextInjectionHook",
        "FinalAnswerVerifierHook",
        "StopPredicateHook",
        "ToolMiddlewareHook",
    }
    for hook in getattr(agent, "hooks", ()) or ():
        module = hook.__class__.__module__
        class_name = hook.__class__.__name__
        policy_id = getattr(hook, "resume_policy_id", getattr(hook, "policy_id", None))
        policy_version = getattr(
            hook,
            "resume_policy_version",
            getattr(hook, "policy_version", None),
        )
        checkpoint_key = getattr(hook, "checkpoint_key", None)
        custom_or_adapter_policy = not module.startswith("linch.") or class_name in adapter_policies
        if custom_or_adapter_policy and not (isinstance(policy_id, str) and policy_id):
            raise ConfigError(
                f"durable runs with custom hook {hook.__class__.__qualname__!r} require "
                "hook.resume_policy_id (plus optional resume_policy_version/"
                "resume_policy_config); checkpoint_key alone identifies state storage, "
                "not policy behavior"
            )
        config = getattr(hook, "resume_policy_config", None)
        if config is None:
            config = getattr(hook, "config", getattr(hook, "_cfg", None))
        identities.append(
            {
                "type": f"{module}.{hook.__class__.__qualname__}",
                "name": getattr(hook, "name", None),
                "checkpoint_key": checkpoint_key,
                "policy_id": policy_id,
                "policy_version": policy_version,
                # Built-in policy hooks expose dataclass configs; the run
                # contract serializer handles those canonically.
                "config": config,
            }
        )
    return identities


def _tool_pipeline_contract(agent: Any) -> dict[str, Any] | None:
    """Return the stable identity of active tool-pipeline policy.

    The pipeline is optional and duck typed so Agent construction remains
    decoupled from the tools package. An inactive pipeline has no execution
    effect and is omitted. Active custom implementations and every listener in
    the built-in pipeline must expose host-stable resume policy metadata.
    """
    pipeline = getattr(agent, "tool_pipeline", None)
    if pipeline is None:
        return None
    has_listeners = getattr(pipeline, "has_listeners", None)
    if not callable(has_listeners) or not has_listeners():
        return None

    policy_id = getattr(pipeline, "resume_policy_id", None)
    policy_version = getattr(pipeline, "resume_policy_version", None)
    if not isinstance(policy_id, str) or not policy_id:
        raise ConfigError(
            "durable runs with an active tool pipeline require "
            "pipeline.resume_policy_id (plus optional resume_policy_version/"
            "resume_policy_config)"
        )
    try:
        config = getattr(pipeline, "resume_policy_config", None)
    except Exception as exc:
        raise ConfigError(f"cannot build durable tool pipeline contract: {exc}") from exc
    if config is None:
        raise ConfigError(
            "durable runs with an active tool pipeline require non-None "
            "pipeline.resume_policy_config"
        )
    try:
        json.dumps(config, allow_nan=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            f"durable tool pipeline resume_policy_config must be JSON-safe: {exc}"
        ) from exc
    return {
        "type": f"{pipeline.__class__.__module__}.{pipeline.__class__.__qualname__}",
        "policy_id": policy_id,
        "policy_version": policy_version,
        "config": config,
    }


def _shell_backend_contract(backend: Any) -> dict[str, Any]:
    """Fingerprint a shell transport, including standalone BashTool execution."""
    policy_id = getattr(backend, "resume_policy_id", None)
    policy_version = getattr(backend, "resume_policy_version", None)
    try:
        config = getattr(backend, "resume_policy_config", None)
    except Exception as exc:
        raise ConfigError(
            f"cannot build durable execution shell backend resume_policy_config: {exc}"
        ) from exc
    if not isinstance(policy_id, str) or not policy_id or config is None:
        raise ConfigError(
            "durable runs with a custom Bash execution backend require "
            "backend.resume_policy_id and non-None backend.resume_policy_config "
            "(plus optional resume_policy_version)"
        )
    return {
        "type": f"{backend.__class__.__module__}.{backend.__class__.__qualname__}",
        "policy_id": policy_id,
        "policy_version": policy_version,
        "config": config,
    }


def _execution_backend_contract(tools: list[Any], configured_backend: Any) -> dict[str, Any] | None:
    """Fingerprint the effective execution world, including shell, fs, and confinement.

    Unified backends can affect filesystem tools even when Bash is not offered,
    so their whole effective world participates in resume identity. A standalone
    ``BashTool(execution=...)`` also fingerprints its effective shell transport.
    """
    bash_tool = next((tool for tool in tools if getattr(tool, "name", None) == "Bash"), None)
    bash_backend = getattr(bash_tool, "_backend", None) if bash_tool is not None else None

    if configured_backend is None:
        return _shell_backend_contract(bash_backend) if bash_backend is not None else None

    shell = configured_backend.shell
    filesystem = configured_backend.fs
    if bash_backend is not None and bash_backend is not shell:
        raise ConfigError(
            "durable execution world mismatch: the effective BashTool backend is not "
            "agent.execution_backend.shell; configure one coherent shell/filesystem world"
        )

    from ..execution.backend import resume_policy_descriptor

    world = resume_policy_descriptor(configured_backend)
    if world is None:
        raise ConfigError(
            "durable runs with a custom execution world require "
            "execution_backend.resume_policy_id and non-None "
            "execution_backend.resume_policy_config (plus optional "
            "resume_policy_version); filesystem overrides must also expose "
            "stable resume policy metadata"
        )
    shell_policy = _shell_backend_contract(shell)
    filesystem_policy = resume_policy_descriptor(filesystem)
    if filesystem_policy is None:
        raise ConfigError(
            "durable runs with a custom execution world require its filesystem backend "
            "to expose resume_policy_id and non-None resume_policy_config"
        )
    try:
        confinement = getattr(configured_backend, "confinement", None)
    except Exception as exc:
        raise ConfigError(f"cannot build durable execution world confinement: {exc}") from exc

    return {
        "world": world,
        "shell": shell_policy,
        "filesystem": filesystem_policy,
        "confinement": confinement,
        "sandboxed": bool(getattr(configured_backend, "sandboxed", False)),
    }


def _build_run_contract(session: Session, prompt: str, opts: RunOptions) -> Any:
    """Build the normalized, resume-safe identity for one run."""
    agent = session.agent
    from ..profiles import _base_run_config, _effective_run_config

    config = (
        _effective_run_config(session, opts.run_id)
        if opts.run_id in session._run_bindings
        else _base_run_config(session)
    )
    registry = _contract_registry(session, config)
    tools = list(registry.list())
    tool_schemas: list[dict[str, Any]] = []
    for tool, schema in zip(tools, registry.schemas(), strict=True):
        tool_schemas.append(
            {
                **schema,
                # Scope is a security property even though providers do not
                # receive it.  A write->read downgrade must invalidate resume.
                "scope": getattr(tool, "scope", None),
                "parallel": bool(getattr(tool, "parallel", True)),
                **(
                    {
                        "output_schema": tool.output_schema,
                        "renderer_id": getattr(tool, "renderer_id", None),
                        "renderer_version": getattr(tool, "renderer_version", None),
                    }
                    if getattr(tool, "output_schema", None) is not None
                    else {}
                ),
            }
        )
    effective_system = config.system_blocks_override
    if effective_system is None:
        effective_system = session.system_blocks_override
    if effective_system is not None:
        system_blocks = list(effective_system)
    else:
        active_names = [tool.name for tool in tools]
        tool_sections = registry.system_prompt_sections(active_names=active_names)
        private_builder = getattr(config, "_build_system_blocks", None)
        builder = getattr(config, "build_system_blocks_for_tool_names", None)
        system_blocks = (
            list(cast(Any, private_builder)(active_names, tool_sections=tool_sections))
            if callable(private_builder)
            else (
                list(cast(Any, builder)(active_names))
                if callable(builder)
                else list(config.system_blocks)
            )
        )
    budget = opts.budget or getattr(session, "inherited_budget", None) or config.budget
    loop_guard = getattr(agent, "loop_guard", None)
    ladder = getattr(agent, "compaction_ladder", None)
    truncation = getattr(agent, "truncation_recovery", None)
    backend_identity = _execution_backend_contract(
        tools,
        getattr(agent, "execution_backend", None),
    )
    policies: dict[str, Any] = {
        "runtime_version": get_version(),
        "model_call_accounting": _accounting_contract(),
        "compaction_skill_listing_fingerprint": _skill_listing_fingerprint(
            getattr(agent, "skill_listing_text", None)
        ),
        "compaction_replay_config": _builtin_compaction_config(getattr(agent, "compaction", None)),
        "permissions": _permission_policy_contract(config),
        "hooks": _hook_policy_contract(config),
        "tool_pipeline": _tool_pipeline_contract(config),
        "features": getattr(agent, "features", None),
        "execution_backend": backend_identity,
        "max_turns": None if config.max_turns == float("inf") else config.max_turns,
        "structured_output_retries": agent.structured_output_retries,
        "loop_guard": loop_guard,
        "compaction": getattr(getattr(agent, "compaction", None), "id", None),
        "compaction_ladder": ladder,
        "truncation_recovery": truncation,
        "reliability": canonical_reliability_policy(getattr(agent, "reliability", None)),
    }
    durability = getattr(agent, "durability", None)
    if getattr(durability, "durable_inbox", False) or _exact_model_input_enabled(agent):
        policies["durability"] = {
            "durable_inbox": bool(getattr(durability, "durable_inbox", False)),
            "exact_model_input": _exact_model_input_enabled(agent),
            **(
                {"model_input_codec_version": MODEL_INPUT_CODEC_VERSION}
                if _exact_model_input_enabled(agent)
                else {}
            ),
        }
    run_options = {
        "max_output_tokens": opts.max_output_tokens or agent.max_output_tokens,
        "temperature": opts.temperature,
        "stream_partials": (
            agent.include_partial_messages if opts.stream_partials is None else opts.stream_partials
        ),
        "thinking": opts.thinking,
        "effort": opts.effort,
        "tool_choice": opts.tool_choice or agent.tool_choice,
        "cache_ttl": agent.cache_ttl,
        "max_retries": agent.max_retries,
    }
    principal_contract = _principal_contract(opts.principal)
    if principal_contract is not None:
        run_options["principal"] = principal_contract
    if opts.profile is not None:
        run_options["profile"] = opts.profile._contract()
    return build_run_contract(
        primary_model=agent.model,
        fallback_models=getattr(agent, "fallback_models", None),
        system_blocks=system_blocks,
        tool_schemas=tool_schemas,
        input_payload={"prompt": prompt, "images": opts.images},
        output_schema=opts.output_schema or config.output_schema,
        final_tool_name=_effective_final_tool_name(agent, opts, config=config),
        run_options=run_options,
        budget=budget,
        policies=policies,
    )
