from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from ..agent import Agent
from ..config import SystemPromptConfig, SystemPromptSection
from ..errors import ConfigError
from ..filesystem.backend import CompositeFileBackend, StateFileBackend
from ..filesystem.sqlite import SqliteFileBackend
from ..hooks import ContextInjectionHook
from ..memory import MemoryContextBuilder, MemorySearchTool, MemoryUpsertTool
from ..run_store import SqliteRunStore
from ..sessions import SqliteSessionStore
from ..tools.registry import default_tools
from ..tools.tasks import TaskCreateTool, TaskGetTool, TaskListTool, TaskUpdateTool
from .prompts import COORDINATOR_SYSTEM_PROMPT, DEEP_AGENT_SYSTEM_PROMPT
from .subagents import DEEP_AGENT_SUBAGENTS

if TYPE_CHECKING:
    from ..config import FeatureFlags
    from ..memory import MemoryStore
    from ..run_store import RunStore
    from ..sessions import SessionStore
    from ..tools import Tool, ToolRegistry

# Tools removed from the coordinator parent so it cannot do heavy work itself.
# Workers still receive full access via build_child_tools in the runner.
_COORDINATOR_EXCLUDED_TOOLS = frozenset(["Edit", "Write", "Bash", "Grep", "Glob", "Read"])


def create_deep_agent(
    *,
    model: str,
    durable: bool = True,
    coordinator: bool = False,
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
    """Create a normal :class:`Agent` with deep-agent defaults.

    This is a distribution layer over the existing Linch runtime. It keeps the
    core loop unchanged while enabling planning tools, specialized subagents,
    virtual filesystem/context-management guidance, optional memory wiring, and
    durable run/session storage.

    When ``coordinator=True``, the agent is configured as a pure orchestrator:
    heavy tools (Edit/Write/Bash/…) are removed from the parent so it delegates
    all real work to worker subagents. A coordinator-specific system prompt is
    injected and ``run_in_background`` becomes the default worker pattern.
    A persistent ``/memories`` filesystem partition is set up for durable state.

    **Open-loop safety rails.** A deep agent is an *open loop*: the model authors
    its own path, so by default it runs unbounded (``max_turns`` is infinite, no
    budget cap). ``loop_guard`` is on by default (it stops pathological repeat/
    failure loops) but does not bound a productive-but-endless exploration. The
    SDK deliberately does **not** fabricate a default cost ceiling or quality
    standard — those are policy you own. Set them explicitly to keep the loop
    honest and affordable:

    - ``budget=RunBudget(...)`` — the cost line; also caps the whole subagent tree.
    - ``verifiers=[...]`` — the standard checked before the final answer; wired
      into the hooks layer for you (``max_verification_retries`` bounds retries).
    - ``max_turns=N`` — a hard length cap.
    """

    if coordinator and features is not None and not getattr(features, "subagents", True):
        raise ConfigError("create_deep_agent(coordinator=True) requires features.subagents=True")

    root = Path(cwd or ".").resolve()
    registry = _deep_agent_tools(
        tools,
        memory_store=memory_store,
        namespace=memory_namespace,
        coordinator=coordinator,
        features=features,
    )
    prompt_config = _merge_deep_agent_prompt(
        system_prompt_config, system_prompt, coordinator=coordinator
    )
    from ..hooks import normalize_hooks as _normalize_hooks

    hooks = _normalize_hooks(agent_kwargs.pop("hooks", None))
    if memory_store is not None:
        hooks.append(
            ContextInjectionHook(MemoryContextBuilder(memory_store, namespace=memory_namespace))
        )
    if verifiers is not None:
        from ..hooks import FinalAnswerVerifierHook

        hooks.append(FinalAnswerVerifierHook(verifiers, max_retries=max_verification_retries))
    agent_kwargs.setdefault("enable_worker_tools", True)
    agent_kwargs.setdefault("retain_subagents", True)
    agent_kwargs.setdefault("enable_background_subagents", True)
    agent_kwargs.setdefault("enable_task_stop", True)

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

    return Agent(
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


def _deep_agent_tools(
    tools: ToolRegistry | None,
    *,
    memory_store: MemoryStore | None,
    namespace: str | None,
    coordinator: bool = False,
    features: FeatureFlags | None = None,
) -> ToolRegistry:
    registry = tools.copy() if tools is not None else default_tools()
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
    if coordinator:
        # Strip heavy tools from the coordinator parent.
        # Workers receive full access through SubagentTool → build_child_tools.
        for name in list(_COORDINATOR_EXCLUDED_TOOLS):
            if registry.get(name) is not None:
                registry.unregister(name)
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
