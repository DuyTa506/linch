from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from ..agent import Agent
from ..budget import RunBudget
from ..config import FeatureFlags, SystemPromptConfig, SystemPromptSection
from ..errors import ConfigError
from ..filesystem.backend import CompositeFileBackend, StateFileBackend
from ..filesystem.sqlite import SqliteFileBackend
from ..hooks import ContextInjectionHook
from ..memory import MemoryContextBuilder, MemorySearchTool, MemoryUpsertTool
from ..run_store import SqliteRunStore
from ..sessions import SqliteSessionStore
from ..tools.registry import workspace_tools
from ..tools.tasks import TaskCreateTool, TaskGetTool, TaskListTool, TaskUpdateTool
from .prompts import COORDINATOR_SYSTEM_PROMPT, DEEP_AGENT_SYSTEM_PROMPT
from .subagents import DEEP_AGENT_SUBAGENTS

if TYPE_CHECKING:
    from ..memory import MemoryStore
    from ..run_store import RunStore
    from ..sessions import SessionStore
    from ..tools import Tool, ToolRegistry

# Tools removed from the coordinator parent so it cannot do heavy work itself.
# Workers still receive full access via build_child_tools in the runner.
_COORDINATOR_EXCLUDED_TOOLS = frozenset(["Edit", "Write", "Bash", "Grep", "Glob", "Read"])


@dataclass(frozen=True, slots=True)
class DeepAgentProfile:
    """Named deep-agent policy bundle.

    Profiles make long-horizon authority, retention, and spending visible and
    reviewable.  Callers can still override individual values on the factory.
    """

    name: str
    durable: bool
    coordinator: bool
    max_turns: int | None
    max_tokens: int | None
    retain_subagents: bool
    enable_background_subagents: bool


DEEP_AGENT_PROFILES: dict[str, DeepAgentProfile] = {
    "balanced": DeepAgentProfile(
        name="balanced",
        durable=True,
        coordinator=False,
        max_turns=64,
        max_tokens=1_000_000,
        retain_subagents=True,
        enable_background_subagents=True,
    ),
    "coordinator": DeepAgentProfile(
        name="coordinator",
        durable=True,
        coordinator=True,
        max_turns=64,
        max_tokens=1_000_000,
        retain_subagents=True,
        enable_background_subagents=True,
    ),
    # Explicit escape hatch for hosts that intentionally own all outer-loop
    # cost/lifetime controls.  A caller must name this profile to get no cap.
    "unbounded": DeepAgentProfile(
        name="unbounded",
        durable=True,
        coordinator=False,
        max_turns=None,
        max_tokens=None,
        retain_subagents=True,
        enable_background_subagents=True,
    ),
}


def _resolve_profile(profile: str | DeepAgentProfile) -> DeepAgentProfile:
    if isinstance(profile, DeepAgentProfile):
        return profile
    try:
        return DEEP_AGENT_PROFILES[profile]
    except (KeyError, TypeError) as exc:
        choices = ", ".join(sorted(DEEP_AGENT_PROFILES))
        raise ConfigError(
            f"unknown deep-agent profile {profile!r}; choose one of: {choices}"
        ) from exc


def create_deep_agent(
    *,
    model: str,
    profile: str | DeepAgentProfile = "balanced",
    durable: bool | None = None,
    coordinator: bool | None = None,
    cwd: str | None = None,
    system_prompt: str | None = None,
    tools: ToolRegistry | None = None,
    permissions: object | dict[str, object] | None = None,
    session_store: SessionStore | None = None,
    run_store: RunStore | None = None,
    features: FeatureFlags | None = None,
    system_prompt_config: SystemPromptConfig | None = None,
    memory_store: MemoryStore | None = None,
    memory_namespace: str | None = None,
    budget: Any = None,
    max_turns: int | None = None,
    verifiers: Any = None,
    max_verification_retries: int = 2,
    **agent_kwargs: Any,
) -> Agent:
    """Create a normal :class:`Agent` from an explicit deep-agent profile.

    This is a distribution layer over the existing Linch runtime. It keeps the
    core loop unchanged while enabling planning tools, specialized subagents,
    virtual filesystem/context-management guidance, optional memory wiring, and
    durable run/session storage.

    When ``coordinator=True``, the agent is configured as a pure orchestrator:
    heavy tools (Edit/Write/Bash/…) are removed from the parent so it delegates
    all real work to worker subagents. A coordinator-specific system prompt is
    injected and ``run_in_background`` becomes the default worker pattern.
    A persistent ``/memories`` filesystem partition is set up for durable state.

    The default ``balanced`` profile is durable, retains workers for continue,
    and bounds the open loop to 64 turns / 1,000,000 tokens across the complete
    subagent tree. Name ``profile="unbounded"`` only when an outer service owns
    equivalent lifetime and cost policy. ``loop_guard`` remains enabled to stop
    pathological repeat/failure loops.

    - ``budget=RunBudget(...)`` — the cost line; also caps the whole subagent tree.
    - ``verifiers=[...]`` — the standard checked before the final answer; wired
      into the hooks layer for you (``max_verification_retries`` bounds retries).
    - ``max_turns=N`` — a hard length cap.

    Args:
        model: Model identifier passed through to Agent.
        profile: Named/profile-object policy bundle. Built-ins are ``balanced``,
            ``coordinator``, and the explicit ``unbounded`` escape hatch.
        durable: Optional override for profile durability.
        coordinator: Optional override for pure-orchestrator mode
            (heavy tools removed, coordinator system prompt, background-worker
            default pattern).
        cwd: Working directory root for the virtual filesystem; defaults to ".".
        system_prompt: Extra system prompt text merged with the deep-agent defaults.
        tools: Existing tool registry to extend, or None for the default set.
        permissions: Permission config passed through to Agent.
        session_store: Session store override; ignored when durable=False.
        run_store: Run store override; ignored when durable=False.
        features: Feature flags; coordinator=True requires features.subagents=True.
        system_prompt_config: System prompt section config to merge with system_prompt.
        memory_store: Memory backend; when set, wires context injection and
            memory tools under memory_namespace.
        memory_namespace: Namespace passed to memory_store and its tools.
        budget: RunBudget cap applied to this agent and its subagent tree.
        max_turns: Hard turn-count override; the profile supplies the default.
        verifiers: Verifiers checked before the final answer is accepted.
        max_verification_retries: Retry cap for failed verifiers.
        **agent_kwargs: Forwarded verbatim to Agent.

    Returns:
        A configured Agent with deep-agent tools, prompt, and (optionally)
        durable storage wired in.
    """

    resolved_profile = _resolve_profile(profile)
    if durable is None:
        durable = resolved_profile.durable
    if coordinator is None:
        coordinator = resolved_profile.coordinator
    if max_turns is None:
        max_turns = resolved_profile.max_turns
    if budget is None and resolved_profile.max_tokens is not None:
        budget = RunBudget(max_tokens=resolved_profile.max_tokens)

    # A deep-agent factory is the explicit opt-in boundary for project-local
    # skills/subagents and its virtual filesystem. MCP stays off unless servers
    # were actually configured, preventing an unrelated ambient connection.
    if features is None:
        has_mcp = bool(agent_kwargs.get("mcp_servers") or agent_kwargs.get("mcpServers"))
        features = FeatureFlags(
            skills=True,
            subagents=True,
            mcp=has_mcp,
            filesystem=True,
        )

    if coordinator and not features.subagents:
        raise ConfigError("create_deep_agent(coordinator=True) requires features.subagents=True")

    root = Path(cwd or ".").resolve()
    worker_registry = _deep_agent_tools(
        tools,
        memory_store=memory_store,
        namespace=memory_namespace,
        features=features,
    )
    registry = worker_registry.copy() if coordinator else worker_registry
    if coordinator:
        for name in _COORDINATOR_EXCLUDED_TOOLS:
            registry.unregister(name)
    prompt_config = _merge_deep_agent_prompt(
        system_prompt_config, system_prompt, coordinator=coordinator
    )
    from ..hooks import normalize_hooks as _normalize_hooks

    hooks = _normalize_hooks(agent_kwargs.pop("hooks", None))
    if memory_store is not None:
        memory_hook = ContextInjectionHook(
            MemoryContextBuilder(memory_store, namespace=memory_namespace)
        )
        # The factory owns this adapter's behavior, so it can make the durable
        # policy identity explicit. The store's changing data is runtime input,
        # like a database queried by a tool; the builder configuration is the
        # resume-critical policy.
        cast(Any, memory_hook).resume_policy_id = "linch.deep-agent.memory-context"
        cast(Any, memory_hook).resume_policy_version = "1"
        cast(Any, memory_hook).resume_policy_config = {
            "namespace": memory_namespace,
            "limit": 5,
        }
        hooks.append(memory_hook)
    if verifiers is not None:
        from ..hooks import FinalAnswerVerifierHook

        hooks.append(FinalAnswerVerifierHook(verifiers, max_retries=max_verification_retries))
    agent_kwargs.setdefault("enable_worker_tools", True)
    agent_kwargs.setdefault("retain_subagents", resolved_profile.retain_subagents)
    agent_kwargs.setdefault(
        "enable_background_subagents", resolved_profile.enable_background_subagents
    )
    agent_kwargs.setdefault("enable_task_stop", True)
    agent_kwargs.setdefault("read_before_write", True)

    if durable:
        store_root = root / ".linch"
        if session_store is None:
            session_store = SqliteSessionStore(store_root / "sessions.db")
        if run_store is None:
            run_store = SqliteRunStore(store_root / "runs.db")
        # Persistent /memories partition: write to /memories/... survives runs.
        # Everything else in the virtual FS stays ephemeral (StateFileBackend).
        if "filesystem" not in agent_kwargs:
            agent_kwargs["filesystem"] = CompositeFileBackend(
                default=StateFileBackend(),
                routes={"/memories": SqliteFileBackend(store_root / "memories.db")},
            )

    agent = Agent(
        model=model,
        cwd=str(root),
        tools=registry,
        permissions=permissions,
        session_store=session_store,
        run_store=run_store,
        features=features,
        system_prompt_config=prompt_config,
        hooks=hooks,
        budget=budget,
        max_turns=max_turns,
        extra_subagents=DEEP_AGENT_SUBAGENTS,
        **agent_kwargs,
    )
    if coordinator:
        agent._set_subagent_tool_registry(worker_registry)
    cast(Any, agent).deep_agent_profile = resolved_profile
    return agent


def _deep_agent_tools(
    tools: ToolRegistry | None,
    *,
    memory_store: MemoryStore | None,
    namespace: str | None,
    features: FeatureFlags | None = None,
) -> ToolRegistry:
    registry = tools.copy() if tools is not None else workspace_tools()
    for tool in (TaskCreateTool(), TaskListTool(), TaskGetTool(), TaskUpdateTool()):
        if registry.get(tool.name) is None:
            registry.register(tool)
    if memory_store is not None:
        for tool in (
            MemorySearchTool(memory_store, namespace=namespace),
            MemoryUpsertTool(memory_store, namespace=namespace),
        ):
            if registry.get(tool.name) is None:
                registry.register(cast("Tool", tool))
    return registry


def _merge_deep_agent_prompt(
    cfg: SystemPromptConfig | None,
    system_prompt: str | None,
    *,
    coordinator: bool = False,
) -> SystemPromptConfig:
    prompt_text = COORDINATOR_SYSTEM_PROMPT if coordinator else DEEP_AGENT_SYSTEM_PROMPT
    section_name = "coordinator" if coordinator else "deep-agent"
    deep_section = SystemPromptSection(
        name=section_name,
        text=prompt_text,
        placement="after_defaults",
    )
    if cfg is None:
        return SystemPromptConfig(
            sections=[deep_section],
            append=system_prompt,
        )

    sections = [deep_section, *(cfg.sections or [])]
    return SystemPromptConfig(
        append=cfg.append if cfg.append is not None else system_prompt,
        blocks=cfg.blocks,
        sections=sections,
        replace_defaults=cfg.replace_defaults,
    )
