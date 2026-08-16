from __future__ import annotations

import asyncio
import os
import platform
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from ._blocking import run_blocking
from ._version import get_version
from .config import FeatureFlags, SystemPromptConfig
from .durability import DurabilityOptions
from .errors import ConfigError
from .openai_responses import OpenAIOptions, OpenAIReasoning
from .permissions import BashRule, CanUseTool, PathRule, PermissionEngine, PermissionRule, ToolRule
from .providers import BaseProvider, OpenAIResponsesProvider, OpenAIResponsesProviderOptions
from .providers.limiter import Limiter, _SemaphoreLimiter
from .recovery import TruncationRecovery
from .sessions import InMemorySessionStore, SessionStore
from .tools import ToolRegistry
from .types import InvokedSkillRecord, Message, PermissionMode, SystemBlock

if TYPE_CHECKING:
    from .run_store import RunStore
    from .session import Session
    from .subagents.types import AgentDefinition
    from .tools import Tool
    from .types import OutputSchema, ToolChoice

# Sentinel for "loop_guard not explicitly provided" — distinguishes the
# default (LoopGuard on) from an explicit None/False (guard disabled).
_UNSET: Any = object()

# Sentinel for "result_offload not explicitly provided" — distinguishes the
# default (offload on, threshold derived from context window) from an explicit
# None (offload disabled).
_DEFAULT_OFFLOAD: Any = object()

_SECTION_PLACEMENTS = ("before_defaults", "after_defaults", "after_env")

_OPEN = "open"
_QUIESCING = "quiescing"
_CLOSED = "closed"


@dataclass(slots=True)
class _PermissionConfig:
    mode: PermissionMode
    rules: list[PermissionRule]
    can_use_tool: CanUseTool | None


@dataclass(slots=True)
class _RuntimeLimits:
    max_tool_concurrency: int
    tool_timeout_ms: float | None


def _resolve_system_prompt(
    system_prompt: str | None,
    system_prompt_alias: str | None,
    cfg: SystemPromptConfig | None,
) -> str | None:
    if system_prompt_alias is not None:
        system_prompt = system_prompt_alias
    if cfg is not None and cfg.append is not None:
        return cfg.append
    return system_prompt


def _normalize_openai_options(
    openai: OpenAIOptions | None,
    *,
    api_key: str | None,
    base_url: str | None,
) -> OpenAIOptions:
    return openai or OpenAIOptions(api_key=api_key, base_url=base_url)


def _resolve_provider(
    provider: BaseProvider | None,
    openai: OpenAIOptions,
    reasoning: OpenAIReasoning | None,
) -> BaseProvider:
    if provider is not None:
        return provider
    return OpenAIResponsesProvider(
        OpenAIResponsesProviderOptions(
            api_key=openai.api_key,
            base_url=openai.base_url,
            default_headers=openai.default_headers,
            reasoning=reasoning,
        )
    )


def _normalize_permissions(permissions: Any | dict[str, object] | None) -> _PermissionConfig:
    if permissions is None:
        return _PermissionConfig(mode="default", rules=[], can_use_tool=None)

    if isinstance(permissions, dict):
        perm_mode = _normalize_permission_mode(permissions.get("mode", "default"))
        rules_raw = permissions.get("rules", [])
        if not isinstance(rules_raw, list):
            raise ConfigError("permissions.rules must be a list")
        perm_rules: list[PermissionRule] = []
        for rule in rules_raw:
            if not isinstance(rule, (ToolRule, PathRule, BashRule)):
                raise ConfigError(
                    "permissions.rules entries must be ToolRule, PathRule, or BashRule"
                )
            perm_rules.append(rule)
        can_use = permissions.get("canUseTool") or permissions.get("can_use_tool")
        return _PermissionConfig(
            mode=perm_mode,
            rules=perm_rules,
            can_use_tool=cast(CanUseTool | None, can_use),
        )

    perm_mode = _normalize_permission_mode(getattr(permissions, "mode", "default"))
    raw_rules = getattr(permissions, "rules", None)
    perm_rules = list(raw_rules) if raw_rules else []
    can_use = getattr(permissions, "canUseTool", None) or getattr(permissions, "can_use_tool", None)
    return _PermissionConfig(
        mode=perm_mode,
        rules=cast(list[PermissionRule], perm_rules),
        can_use_tool=cast(CanUseTool | None, can_use),
    )


def _resolve_runtime_limits(
    max_tool_concurrency: int | None,
    tool_timeout_ms: float | None,
) -> _RuntimeLimits:
    env_concurrency = os.getenv("AGENTKIT_MAX_TOOL_CONCURRENCY")
    if max_tool_concurrency is None:
        if env_concurrency is not None:
            try:
                max_tool_concurrency = int(float(env_concurrency))
            except (ValueError, TypeError):
                pass
        if max_tool_concurrency is None:
            max_tool_concurrency = os.cpu_count() or 4

    env_timeout = os.getenv("AGENTKIT_TOOL_TIMEOUT_MS")
    if tool_timeout_ms is None and env_timeout is not None:
        try:
            tool_timeout_ms = float(env_timeout)
        except ValueError:
            pass

    timeout = tool_timeout_ms if tool_timeout_ms is not None and tool_timeout_ms > 0 else None
    return _RuntimeLimits(
        max_tool_concurrency=max(1, int(max_tool_concurrency)),
        tool_timeout_ms=timeout,
    )


def _resolve_limiter(
    limiter: Limiter | None,
    max_provider_concurrency: int | None,
) -> Limiter | None:
    """Pick the single provider gate, or ``None`` for the zero-overhead default."""
    if limiter is not None and max_provider_concurrency is not None:
        raise ConfigError(
            "pass either limiter= or max_provider_concurrency=, not both; "
            "max_provider_concurrency is a shortcut that builds a limiter for you"
        )
    if limiter is not None:
        return limiter
    if max_provider_concurrency is None:
        return None
    return _SemaphoreLimiter(max(1, int(max_provider_concurrency)))


def _offload_threshold_was_auto(result_offload: Any) -> bool:
    """Whether the offload threshold will be auto-derived from the context window.

    True for the default config and any ``OffloadConfig`` left with
    ``threshold_tokens=None``; False when offloading is disabled or the threshold
    is set explicitly. Used to decide whether ``provider.prepare()`` may refresh
    it.
    """
    if result_offload is _DEFAULT_OFFLOAD:
        return True
    if result_offload is None:
        return False
    return getattr(result_offload, "threshold_tokens", None) is None


def _resolve_result_offload(result_offload: Any, provider: BaseProvider, model: str) -> Any:
    if result_offload is _DEFAULT_OFFLOAD:
        from .filesystem.offload import OffloadConfig as _OffloadConfig

        result_offload = _OffloadConfig()

    if result_offload is not None and getattr(result_offload, "threshold_tokens", None) is None:
        try:
            import dataclasses as _dc

            fraction = getattr(result_offload, "threshold_fraction", 0.1)
            ctx_window = provider.context_window(model)
            resolved = max(1_000, int(ctx_window * fraction))
            result_offload = _dc.replace(result_offload, threshold_tokens=resolved)
        except Exception as _exc:
            import logging as _logging

            _logging.getLogger(__name__).warning(
                "Could not resolve threshold_tokens from provider.context_window(%r): %s. "
                "Result offloading will be skipped. Pass an explicit "
                "OffloadConfig(threshold_tokens=N) to suppress this warning.",
                model,
                _exc,
            )
    return result_offload


def _resolve_tool_cache(value: Any) -> Any:
    """Normalize the ``tool_cache`` arg into a ToolCacheHook or ``None`` (off).

    * ``None`` / ``False`` → disabled.
    * ``True`` → cache every read-scope tool.
    * ``ToolCacheConfig`` → cache per that config.
    * a ``ToolCacheHook`` (anything exposing ``on_pre_tool_use``) → used as-is.
    """
    if value is None or value is False:
        return None
    from .hooks.tool_cache import ToolCacheConfig, ToolCacheHook

    if value is True:
        return ToolCacheHook()
    if isinstance(value, ToolCacheConfig):
        return ToolCacheHook(value)
    if hasattr(value, "on_pre_tool_use"):
        return value
    raise TypeError(
        "tool_cache must be a ToolCacheConfig, ToolCacheHook, True, None, or False; "
        f"got {type(value)!r}"
    )


def _resolve_read_before_write(value: Any) -> Any:
    """Normalize ``read_before_write`` into a safety hook or ``None``."""
    if value is None or value is False:
        return None
    from .hooks.read_before_write import ReadBeforeWriteConfig, ReadBeforeWriteHook

    if value is True:
        return ReadBeforeWriteHook()
    if isinstance(value, ReadBeforeWriteConfig):
        return ReadBeforeWriteHook(value)
    if isinstance(value, ReadBeforeWriteHook):
        return value
    if hasattr(value, "on_pre_tool_use") and hasattr(value, "on_post_tool_use"):
        return value
    raise TypeError(
        "read_before_write must be True, False, ReadBeforeWriteConfig, "
        f"or ReadBeforeWriteHook; got {type(value)!r}"
    )


def _has_read_before_write_hook(hooks: list[Any]) -> bool:
    # Match by type, not by name string: an unrelated hook that happens to be
    # named "read_before_write" must not silently suppress the default guard.
    from .hooks.read_before_write import ReadBeforeWriteHook

    return any(isinstance(hook, ReadBeforeWriteHook) for hook in hooks)


def _system_prompt_section_blocks(
    cfg: SystemPromptConfig | None,
    tool_sections: list[Any] | None = None,
) -> dict[str, list[SystemBlock]]:
    grouped: dict[str, list[SystemBlock]] = {placement: [] for placement in _SECTION_PLACEMENTS}
    sections = [*((cfg.sections or []) if cfg is not None else []), *(tool_sections or [])]
    for section in sections:
        placement = getattr(section, "placement", "before_defaults")
        if placement not in grouped:
            raise ConfigError(
                "system_prompt_config.sections placement must be one of "
                f"{', '.join(_SECTION_PLACEMENTS)}"
            )
        name = getattr(section, "name", "")
        text = getattr(section, "text", "")
        if not isinstance(name, str) or name.strip() == "":
            raise ConfigError("system_prompt_config.sections name must be a non-empty string")
        if not isinstance(text, str) or text == "":
            raise ConfigError("system_prompt_config.sections text must be a non-empty string")
        grouped[placement].append(
            SystemBlock(
                text=text,
                cacheable=bool(getattr(section, "cacheable", True)),
            )
        )
    return grouped


# Domain-neutral identity for the SDK core.  Coding identity and operating
# doctrine belong to explicit presets (for example ``create_deep_agent``).
_CORE_IDENTITY = (
    "You are an AI assistant configured through the Linch SDK. Follow the "
    "user's instructions and the configured policies. Use only capabilities "
    "that are actually available, keep claims grounded in observed results, "
    "and state material uncertainty instead of guessing."
)

# Tools whose presence marks an agent as doing software-engineering work.
_SWE_TOOL_FAMILIES = {"Read", "Edit", "Write", "Glob", "Grep", "Bash"}


def _build_tool_protocol_lines(
    present: set[str], *, has_swe_tools: bool, bash_sandboxed: bool
) -> list[str]:
    """Build the tool-use-protocol bullet lines for the tools actually present.

    Only clauses for present tools are emitted so non-SWE agents (RAG, SQL, …)
    aren't given misleading file/shell instructions."""
    lines: list[str] = []
    if {"Read", "Edit"} & present:
        lines.append("- Read a file before you Edit it. The Edit tool will refuse if you have not.")
        lines.append(
            "- Edits require an exact byte-for-byte match of the old_string "
            "in the current file contents, including indentation."
        )
    if {"Write", "Edit"} & present:
        lines.append(
            "- Prefer Edit over Write when modifying an existing file. Use "
            "Write only for new files or full rewrites."
        )
    if {"Glob", "Grep"} & present:
        lines.append(
            "- Glob is for finding files by name pattern; Grep is for "
            "searching file contents. They are read-only."
        )
    if "Bash" in present:
        if bash_sandboxed:
            lines.append(
                "- Bash runs inside a sandbox. Commands are isolated from the host environment."
            )
        else:
            lines.append(
                "- Bash runs in the user's environment with full permissions. "
                "There is no sandbox. Avoid commands that change global state "
                "unless the user asked for them."
            )
    # Always include the generic parallel-tool hint when any tools exist.
    if present:
        if has_swe_tools:
            lines.append(
                "- Issue multiple tool calls in a single turn when they are "
                "independent. Read, Glob, and Grep can run concurrently."
            )
        else:
            lines.append("- Issue multiple tool calls in a single turn when they are independent.")
    return lines


@dataclass(slots=True)
class AgentOptions:
    model: str
    provider: BaseProvider | None = None
    openai: OpenAIOptions = field(default_factory=OpenAIOptions)
    reasoning: OpenAIReasoning | None = None
    tools: ToolRegistry | None = None
    permissions: dict[str, object] | None = None
    session_store: SessionStore | None = None
    run_store: RunStore | None = None
    durability: DurabilityOptions | None = None
    cwd: str | None = None
    system_prompt: str | None = None
    system_prompt_config: SystemPromptConfig | None = None
    max_retries: int = 5
    max_output_tokens: int | None = None
    include_partial_messages: bool = False
    max_turns: int | None = None
    max_tool_concurrency: int | None = None
    limiter: Any = None  # Limiter | None
    max_provider_concurrency: int | None = None
    tool_batching_strategy: str = "greedy"
    tool_timeout_ms: float | None = None
    tool_retry: Any = None  # RetryOptions | None
    cache_ttl: str | None = None
    config_dir: str | None = None
    mcp_servers: dict[str, Any] | None = None
    compaction: Any = None
    compaction_ladder: Any = None  # CompactionLadder | None
    truncation_recovery: TruncationRecovery | None = None
    token_estimator: Any = None
    budget: Any = None  # RunBudget | None
    features: FeatureFlags | None = None
    deps: Any = None
    output_schema: Any = None  # OutputSchema | None
    tool_choice: Any = None  # ToolChoice | None
    final_tool_name: str | None = None
    loop_guard: Any = None  # LoopGuard | None; None means "use default LoopGuard"
    filesystem: Any = None  # FileBackend | None
    result_offload: Any = None  # OffloadConfig | None; requires filesystem feature opt-in
    hooks: Any = None
    read_before_write: Any = False
    extra_subagents: list[AgentDefinition] | None = None
    enable_worker_tools: bool = False
    retain_subagents: bool = False
    enable_background_subagents: bool = False
    enable_task_stop: bool = False


class Agent:
    def __init__(
        self,
        *,
        provider: BaseProvider | None = None,
        model: str,
        openai: OpenAIOptions | None = None,
        reasoning: OpenAIReasoning | None = None,
        openai_api_key: str | None = None,
        openai_base_url: str | None = None,
        tools: ToolRegistry | None = None,
        permissions: Any | dict[str, object] | None = None,
        session_store: SessionStore | None = None,
        run_store: RunStore | None = None,
        durability: DurabilityOptions | None = None,
        cwd: str | None = None,
        system_prompt: str | None = None,
        systemPrompt: str | None = None,
        system_prompt_config: SystemPromptConfig | None = None,
        max_retries: int = 5,
        maxRetries: int | None = None,
        max_output_tokens: int | None = None,
        maxOutputTokens: int | None = None,
        include_partial_messages: bool = False,
        includePartialMessages: bool | None = None,
        max_turns: int | None = None,
        maxTurns: int | None = None,
        max_tool_concurrency: int | None = None,
        maxToolConcurrency: int | None = None,
        limiter: Limiter | None = None,
        max_provider_concurrency: int | None = None,
        tool_batching_strategy: str = "greedy",
        toolBatchingStrategy: str | None = None,
        tool_timeout_ms: float | None = None,
        toolTimeoutMs: float | None = None,
        tool_retry: Any = None,
        cache_ttl: str | None = None,
        cacheTtl: str | None = None,
        config_dir: str | None = None,
        configDir: str | None = None,
        mcp_servers: dict[str, Any] | None = None,
        mcpServers: dict[str, Any] | None = None,
        compaction: Any = None,
        compaction_ladder: Any = None,
        truncation_recovery: TruncationRecovery | None = None,
        token_estimator: Any = None,
        fallback_models: list[str] | None = None,
        budget: Any = None,
        features: FeatureFlags | None = None,
        deps: Any = None,
        output_schema: OutputSchema | None = None,
        structured_output_retries: int = 0,
        tool_choice: ToolChoice | None = None,
        final_tool_name: str | None = None,
        loop_guard: Any = _UNSET,
        loopGuard: Any = _UNSET,
        filesystem: Any = None,
        result_offload: Any = _DEFAULT_OFFLOAD,
        mailbox: Any = None,
        schedule_store: Any = None,
        hooks: Any = None,
        read_before_write: Any = False,
        tool_cache: Any = None,
        extra_subagents: list[AgentDefinition] | None = None,
        enable_worker_tools: bool = False,
        retain_subagents: bool = False,
        enable_background_subagents: bool = False,
        enable_task_stop: bool = False,
        enable_background_tools: bool = False,
        execution_backend: Any = None,
        ask_user: Any = None,
    ) -> None:
        system_prompt = _resolve_system_prompt(system_prompt, systemPrompt, system_prompt_config)
        if maxRetries is not None:
            max_retries = maxRetries
        if maxOutputTokens is not None:
            max_output_tokens = maxOutputTokens
        if includePartialMessages is not None:
            include_partial_messages = includePartialMessages
        if maxTurns is not None:
            max_turns = maxTurns
        if maxToolConcurrency is not None:
            max_tool_concurrency = maxToolConcurrency
        if toolBatchingStrategy is not None:
            tool_batching_strategy = toolBatchingStrategy
        if tool_batching_strategy not in ("greedy", "maximal"):
            raise ConfigError('tool_batching_strategy must be "greedy" or "maximal"')
        if toolTimeoutMs is not None:
            tool_timeout_ms = toolTimeoutMs
        if cacheTtl is not None:
            cache_ttl = cacheTtl
        if configDir is not None:
            config_dir = configDir
        if mcpServers is not None:
            mcp_servers = mcpServers

        if not model:
            raise ConfigError("Agent requires a model")
        if max_retries < 0:
            raise ConfigError("max_retries must be non-negative")
        if truncation_recovery is not None and not isinstance(
            truncation_recovery, TruncationRecovery
        ):
            raise ConfigError("truncation_recovery must be TruncationRecovery or None")
        if durability is not None and not isinstance(durability, DurabilityOptions):
            raise ConfigError("durability must be DurabilityOptions or None")

        openai = _normalize_openai_options(
            openai,
            api_key=openai_api_key,
            base_url=openai_base_url,
        )
        permissions_config = _normalize_permissions(permissions)
        provider = _resolve_provider(provider, openai, reasoning)
        runtime_limits = _resolve_runtime_limits(max_tool_concurrency, tool_timeout_ms)

        cwd_resolved = str(Path(cwd or os.getcwd()).resolve())
        self.model = model
        self.cwd = cwd_resolved
        # Linch is an SDK, not an implicit coding harness.  A bare Agent gets
        # no shell/filesystem authority; applications opt in with their own
        # registry or ``workspace_tools()``/a higher-level preset.
        self.tools = tools if tools is not None else ToolRegistry()
        self.execution_backend = execution_backend
        if execution_backend is not None:
            from .tools.builtin import BashTool

            if self.tools.get("Bash") is not None:
                self.tools.replace(BashTool(backend=execution_backend))
        if ask_user is not None:
            from .tools.ask_user import AskUserTool

            self.tools.replace(AskUserTool(ask_user))
        self.permission_engine = PermissionEngine(
            mode=permissions_config.mode,
            rules=permissions_config.rules,
            can_use_tool=permissions_config.can_use_tool,
            project_root=cwd_resolved,
        )
        self._store: SessionStore | None = session_store
        self.run_store: RunStore | None = run_store
        self.durability: DurabilityOptions = durability or DurabilityOptions()
        self.system_prompt = system_prompt
        self.max_retries = max_retries
        self.max_output_tokens = max_output_tokens
        self.include_partial_messages = include_partial_messages
        self.max_turns = max_turns or float("inf")
        self._provider = provider
        self.cache_ttl = cache_ttl
        self._config_dir = str(Path(cwd_resolved) / (config_dir or ".linch"))
        self._mcp_servers = mcp_servers
        self.max_tool_concurrency = runtime_limits.max_tool_concurrency
        self.tool_concurrency = self.max_tool_concurrency
        self.limiter: Limiter | None = _resolve_limiter(limiter, max_provider_concurrency)
        self.tool_batching_strategy = tool_batching_strategy
        self.tool_timeout_ms: float | None = runtime_limits.tool_timeout_ms

        # Optional tool retry config (RetryOptions | None; None = no retry by default).
        self.tool_retry: Any = tool_retry

        self._initialize_extension_state(
            extra_subagents=extra_subagents,
            enable_worker_tools=enable_worker_tools,
            retain_subagents=retain_subagents,
            enable_background_subagents=enable_background_subagents,
            enable_task_stop=enable_task_stop,
        )
        self.enable_background_tools = bool(enable_background_tools)
        self.compaction: Any = compaction
        # Opt-in micro/reactive compaction rungs (CompactionLadder | None).
        # None keeps the legacy single-retry behavior byte-identical.
        self.compaction_ladder: Any = compaction_ladder
        # Opt-in output-truncation recovery (TruncationRecovery | None). None
        # keeps the default behavior (truncated text becomes the final answer)
        # byte-identical; Linch never escalates the output cap implicitly.
        self.truncation_recovery: TruncationRecovery | None = truncation_recovery
        self.token_estimator = token_estimator
        # Ordered alternate models tried, in turn, when the active model
        # overloads mid-run (ProviderError(retryable=True)). None/[] = disabled
        # (default byte-identical).
        self.fallback_models: list[str] | None = fallback_models
        # Default spending cap shared by every session/run of this agent.
        # Prefer RunOptions(budget=...) for per-run caps.
        self.budget: Any = budget

        # Feature flags (controls which subsystems connect in session())
        self.features: FeatureFlags = features or FeatureFlags()

        # App-state dependency object threaded into ToolContext.deps
        self.deps: Any = deps

        # Output contracting defaults (can be overridden per-run via RunOptions)
        self.output_schema: OutputSchema | None = output_schema
        self.tool_choice: ToolChoice | None = tool_choice
        self.final_tool_name: str | None = final_tool_name
        self.structured_output_retries = max(0, int(structured_output_retries))

        # Store SystemPromptConfig for use in _build_system_blocks
        self._system_prompt_config: SystemPromptConfig | None = system_prompt_config
        self._cached_system_blocks: list[SystemBlock] | None = None
        self._cached_system_blocks_signature: tuple[Any, ...] | None = None
        self._configure_loop_guard(loop_guard, loopGuard)
        self._configure_hooks(
            hooks=hooks,
            read_before_write=read_before_write,
            tool_cache=tool_cache,
        )
        self._configure_filesystem(filesystem, result_offload)
        self._configure_mailbox(mailbox)
        self._configure_schedule_store(schedule_store)

    def _initialize_extension_state(
        self,
        *,
        extra_subagents: list[AgentDefinition] | None,
        enable_worker_tools: bool,
        retain_subagents: bool,
        enable_background_subagents: bool,
        enable_task_stop: bool,
    ) -> None:
        self.skills: dict[str, Any] = {}
        self.skill_listing_text: str | None = None
        self._sessions: dict[str, Session] = {}
        self.subagent_registry: Any = None
        self.subagent_run_counters: dict[str, int] = {}
        self.extra_subagents: list[AgentDefinition] = list(extra_subagents or [])
        self.enable_worker_tools = bool(enable_worker_tools)
        self.retain_subagents = bool(retain_subagents)
        self.enable_background_subagents = bool(enable_background_subagents)
        self.enable_task_stop = bool(enable_task_stop)
        self._skills_connect: Any = None
        self._skills_loaded: bool = False
        self._subagents_connect: Any = None
        self._subagents_loaded: bool = False
        # Coordinator mode can expose a restricted registry to the parent while
        # retaining a fuller catalog for workers.  ``None`` preserves the
        # historical behavior: children derive tools directly from
        # ``self.tools`` (or their parent session's narrower override).
        self._subagent_tool_registry: ToolRegistry | None = None
        self._mcp_connect: Any = None
        self._mcp_connection: Any = None
        # Additional MCP connections attached mid-run (closed alongside the
        # primary connection in close()).
        self._extra_mcp_connections: list[Any] = []

    def _set_subagent_tool_registry(self, registry: ToolRegistry) -> None:
        """Install a worker-only tool catalog without changing parent tools.

        This private seam is used by coordinator mode.  The caller-supplied
        registry is copied so later parent registration (Subagent,
        SubagentContinue, TaskStop) cannot leak orchestration tools into child
        sessions.  Tools configured by ``Agent.__init__`` — virtual filesystem,
        mailbox, schedule, AskUser, and similar additions — are merged into the
        worker catalog before the first child can run.
        """

        worker_tools = registry.copy()
        if self.execution_backend is not None and worker_tools.get("Bash") is not None:
            from .tools.builtin import BashTool

            worker_tools.replace(BashTool(backend=self.execution_backend))
        for tool in self.tools.list():
            if worker_tools.get(tool.name) is None:
                worker_tools.register(tool)
        self._subagent_tool_registry = worker_tools

    def _configure_loop_guard(self, loop_guard: Any, loop_guard_alias: Any) -> None:
        from .loop_guard import LoopGuard as _LoopGuard
        from .loop_guard import normalize_loop_guard as _normalize_loop_guard

        effective_lg = loop_guard_alias if loop_guard_alias is not _UNSET else loop_guard
        if effective_lg is _UNSET:
            self.loop_guard: _LoopGuard | None = _LoopGuard()
        else:
            self.loop_guard = _normalize_loop_guard(effective_lg)

    def _configure_hooks(
        self,
        *,
        hooks: Any,
        read_before_write: Any = False,
        tool_cache: Any = None,
    ) -> None:
        from .hooks import normalize_hooks

        self._hooks: list[Any] = normalize_hooks(hooks)
        rbw_hook = _resolve_read_before_write(read_before_write)
        if rbw_hook is not None and not _has_read_before_write_hook(self._hooks):
            self._hooks.append(rbw_hook)
        # Tool-result cache is opt-in. Append it LAST so it keys on the input
        # after any user PreToolUse hook has mutated it, and serves only once
        # those hooks have run. Replacing `agent.hooks` wholesale drops it.
        cache_hook = _resolve_tool_cache(tool_cache)
        if cache_hook is not None:
            self._hooks.append(cache_hook)
        # Accumulates hooks removed by the hooks setter so Agent.close() can
        # still call their close/aclose methods and avoid resource leaks.
        self._replaced_hooks: list[Any] = []

        # One-time, coalesced provider preparation (e.g. llama.cpp /props probe)
        # awaited before the first run. The lock is created lazily inside the
        # running loop so N agents never share loop-bound state.
        self._provider_prepared: bool = False
        self._prepare_lock: asyncio.Lock | None = None

        # Transactional lifecycle state. Locks/tasks are created lazily inside a
        # running loop so constructing an Agent never binds it to an event loop.
        self._lifecycle_state: str = _OPEN
        self._closed: bool = False
        self._close_task: asyncio.Task[None] | None = None
        self._session_lock: asyncio.Lock | None = None
        self._teardown_complete: set[str] = set()
        self._closed_hook_ids: set[int] = set()

    def _configure_filesystem(self, filesystem: Any, result_offload: Any) -> None:
        self._filesystem_default: Any = filesystem
        # An auto-derived offload threshold (a fraction of the context window) is
        # refreshed after a successful provider.prepare(); an explicit
        # threshold_tokens is left untouched.
        self._offload_threshold_auto: bool = _offload_threshold_was_auto(result_offload)
        self.result_offload: Any = _resolve_result_offload(
            result_offload,
            self.provider,
            self.model,
        )
        if not self._filesystem_active():
            return

        from .filesystem.tools import filesystem_tools as _fs_tools

        for t in _fs_tools():
            try:
                self.tools.register(t)
            except Exception:
                pass  # already registered (e.g. caller added them manually)
        self._refresh_system_blocks()

    def _configure_mailbox(self, mailbox: Any) -> None:
        """Register the peer-messaging tool when a mailbox is provided.

        Opt-in: with ``mailbox=None`` no tool is added and no drain runs, so the
        loop stays byte-identical. Workers share this same ``Agent`` object, so
        ``self.mailbox`` is reachable from every session in the agent tree.
        """
        self.mailbox: Any = mailbox
        if mailbox is None:
            return

        from .coordination.send_message import SendMessageTool

        get_session = lambda sid: self._sessions.get(sid)  # noqa: E731
        try:
            self.tools.register(
                cast("Tool", SendMessageTool(mailbox=mailbox, get_session=get_session))
            )
        except ConfigError:
            pass  # already registered (e.g. caller added it manually)
        self._refresh_system_blocks()

    def _configure_schedule_store(self, schedule_store: Any) -> None:
        """Register the schedule create/list/cancel tools when a store is provided.

        Opt-in: with ``schedule_store=None`` no tool is added and the loop stays
        byte-identical. The embedder drives a ``SchedulerLoop`` over the same
        store to fire due schedules into a session.
        """
        self.schedule_store: Any = schedule_store
        if schedule_store is None:
            return

        from .coordination.scheduling import schedule_tools

        for schedule_tool in schedule_tools(schedule_store):
            try:
                self.tools.register(cast("Tool", schedule_tool))
            except ConfigError:
                pass  # already registered (e.g. caller added it manually)
        self._refresh_system_blocks()

    def _filesystem_active(self) -> bool:
        """Return True when the virtual filesystem subsystem should be on."""
        enabled = getattr(self.features, "filesystem", True)
        return bool(enabled) and (
            self._filesystem_default is not None or self.result_offload is not None
        )

    @property
    def system_blocks(self) -> list[SystemBlock]:
        tool_names = sorted(tool.name for tool in self.tools.list())
        tool_sections = self.tools.system_prompt_sections(active_names=tool_names)
        signature = self._system_blocks_signature(tool_names, tool_sections)
        if (
            self._cached_system_blocks is not None
            and self._cached_system_blocks_signature == signature
        ):
            return self._cached_system_blocks
        blocks = self._build_system_blocks(tool_names, tool_sections=tool_sections)
        self._cached_system_blocks = blocks
        self._cached_system_blocks_signature = signature
        return blocks

    def _refresh_system_blocks(self) -> None:
        self._cached_system_blocks = None
        self._cached_system_blocks_signature = None

    def _system_blocks_signature(
        self,
        tool_names: list[str],
        tool_sections: list[Any],
    ) -> tuple[Any, ...]:
        """Return the stable identity of every static prompt input.

        Tool-contributed section content is part of this key, so changing a
        dynamic contribution invalidates the cached provider prefix even when
        the registered tool names and JSON schemas did not change.
        """
        cfg = self._system_prompt_config
        cfg_sections = tuple(
            (
                getattr(section, "name", None),
                getattr(section, "text", None),
                getattr(section, "cacheable", None),
                getattr(section, "placement", None),
            )
            for section in ((cfg.sections or []) if cfg is not None else [])
        )
        contributed = tuple(
            (section.name, section.text, section.cacheable, section.placement)
            for section in tool_sections
        )
        cfg_blocks = tuple(
            (getattr(block, "text", None), getattr(block, "cacheable", None))
            for block in ((cfg.blocks or []) if cfg is not None else [])
        )
        return (
            self.tools.generation,
            tuple(tool_names),
            contributed,
            cfg_sections,
            cfg_blocks,
            getattr(cfg, "replace_defaults", False),
            getattr(cfg, "append", None),
            self.system_prompt,
            self.permission_engine.mode,
            self.execution_backend is not None,
        )

    def _get_store(self) -> SessionStore:
        if self._store is None:
            # A neutral SDK instance must not create project-local state merely
            # because a caller opened a session. Durable presets and services
            # pass an explicit store.
            self._store = InMemorySessionStore()
        return self._store

    @property
    def session_store(self) -> SessionStore | None:
        return self._store

    @session_store.setter
    def session_store(self, value: SessionStore | None) -> None:
        self._store = value

    @property
    def hooks(self) -> list[Any]:
        return self._hooks

    @hooks.setter
    def hooks(self, value: Any) -> None:
        from .hooks import normalize_hooks

        new_hooks = normalize_hooks(value)
        # Track replaced hooks so Agent.close() can still flush/close them.
        removed = [h for h in self._hooks if h not in new_hooks]
        self._replaced_hooks.extend(removed)
        self._hooks = new_hooks

    def context_window(self) -> int:
        return self.provider.context_window(self.model)

    async def _ensure_provider_prepared(self) -> None:
        """Run the provider's optional one-time ``prepare()`` before the first run.

        Duck-typed and coalesced: providers without ``prepare()`` are skipped, the
        hook runs at most once across concurrent first-runs, and a failing probe
        never aborts the run. After a successful prepare, offload thresholds
        derived from the context window are refreshed.
        """
        if self._provider_prepared:
            return
        prepare = getattr(self._provider, "prepare", None)
        if prepare is None:
            self._provider_prepared = True
            return
        if self._prepare_lock is None:
            self._prepare_lock = asyncio.Lock()
        async with self._prepare_lock:
            if self._provider_prepared:
                return
            try:
                await prepare()
            except Exception as exc:
                import logging as _logging

                _logging.getLogger(__name__).warning(
                    "provider.prepare() failed; using configured defaults: %s", exc
                )
            else:
                self._refresh_context_window_derived()
            finally:
                self._provider_prepared = True

    def _refresh_context_window_derived(self) -> None:
        """Recompute values derived from the context window after preparation."""
        if not getattr(self, "_offload_threshold_auto", False) or self.result_offload is None:
            return
        import dataclasses as _dc

        try:
            ctx_window = self.provider.context_window(self.model)
        except Exception:
            return
        fraction = getattr(self.result_offload, "threshold_fraction", 0.1)
        self.result_offload = _dc.replace(
            self.result_offload, threshold_tokens=max(1_000, int(ctx_window * fraction))
        )

    @property
    def provider(self) -> BaseProvider:
        return self._provider

    @provider.setter
    def provider(self, value: BaseProvider) -> None:
        self._provider = value
        self._reset_provider_prepared()

    @property
    def openai(self) -> BaseProvider:
        # Backward compatibility for older integrations.
        return self._provider

    @openai.setter
    def openai(self, value: BaseProvider) -> None:
        self._provider = value
        self._reset_provider_prepared()

    def _reset_provider_prepared(self) -> None:
        """Reset one-time prepare-state after the provider is reassigned so the
        replacement provider's ``prepare()`` runs before the next run and its
        context-window-derived values are re-derived."""
        self._provider_prepared = False
        self._prepare_lock = None
        if getattr(self, "result_offload", None) is not None:
            self._refresh_context_window_derived()

    def _build_system_blocks(
        self,
        tool_names: list[str],
        *,
        tool_sections: list[Any] | None = None,
    ) -> list[SystemBlock]:
        names = ", ".join(sorted(tool_names))
        shell = os.environ.get("SHELL", os.environ.get("COMSPEC", "unknown"))
        py_ver = platform.python_version()
        os_info = f"{platform.system()} {platform.release()} ({platform.machine()})"

        # ── Tool-aware protocol block ────────────────────────────────────────
        # Only include clauses for the tools that are actually present so
        # non-SWE agents (RAG, SQL, …) aren't given misleading instructions.
        present = set(tool_names)
        has_swe_tools = bool(present & _SWE_TOOL_FAMILIES)
        protocol_lines = _build_tool_protocol_lines(
            present,
            has_swe_tools=has_swe_tools,
            bash_sandboxed=self.execution_backend is not None,
        )

        # ── env block (always included) ──────────────────────────────────────
        env_lines = [
            "Runtime:",
            "",
            f"- Linch version: {get_version()}",
            f"- Permission mode: {self.permission_engine.mode}",
            f"- Tools available: {names or 'none'}",
        ]
        if has_swe_tools:
            env_lines.extend(
                [
                    f"- Working directory: {self.cwd}",
                    f"- OS: {os_info}",
                    f"- Shell: {shell}",
                    f"- Python: {py_ver}",
                ]
            )
        env_text = "\n".join(env_lines)

        cfg = self._system_prompt_config

        # ── Assemble blocks ──────────────────────────────────────────────────
        blocks: list[SystemBlock] = []
        section_blocks = _system_prompt_section_blocks(cfg, tool_sections)

        if cfg is not None and cfg.replace_defaults:
            # Custom-identity / non-SWE mode: skip built-in identity + protocol
            blocks.extend(section_blocks["before_defaults"])
            if cfg.blocks:
                blocks.extend(cfg.blocks)
            blocks.extend(section_blocks["after_defaults"])
        else:
            # Neutral SDK mode: caller/tool blocks surround a generic identity.
            blocks.extend(section_blocks["before_defaults"])
            if cfg is not None and cfg.blocks:
                blocks.extend(cfg.blocks)
            blocks.append(SystemBlock(text=_CORE_IDENTITY, cacheable=True))
            if has_swe_tools and protocol_lines:
                protocol = "Tool use protocol:\n\n" + "\n".join(protocol_lines)
                blocks.append(SystemBlock(text=protocol, cacheable=True))
            elif protocol_lines:
                # Non-SWE tools present: include the generic concurrency hint only
                protocol = "Tool use protocol:\n\n" + protocol_lines[-1]
                blocks.append(SystemBlock(text=protocol, cacheable=True))
            blocks.extend(section_blocks["after_defaults"])

        # ── Virtual filesystem block ─────────────────────────────────────────
        # Present whenever the filesystem tools are available, in both default
        # and replace_defaults modes — offloaded RAG/search results are useless
        # if the model doesn't know how to recover them.
        if {"read_file", "ls", "write_file", "edit_file"} & present:
            fs_lines = [
                "Virtual filesystem:",
                "",
                "- You have a virtual filesystem, separate from the real workspace, "
                "accessed via ls, read_file, write_file, and edit_file.",
                "- Large tool results may be automatically offloaded here: instead of the "
                "full output you will see a short preview plus a file path. Call "
                "read_file(path, offset, limit) to read the parts you need — do not "
                "assume the preview is the whole result.",
                "- Use write_file as a scratchpad for notes, plans, or intermediate "
                "results you want to keep across turns without bloating the conversation.",
            ]
            blocks.append(SystemBlock(text="\n".join(fs_lines), cacheable=True))

        # env_text is always present
        blocks.append(SystemBlock(text=env_text, cacheable=True))
        blocks.extend(section_blocks["after_env"])

        # User-provided instructions (from system_prompt or SystemPromptConfig.append)
        append_text = self.system_prompt
        if cfg is not None and cfg.append is not None:
            append_text = cfg.append
        if append_text:
            blocks.append(
                SystemBlock(
                    text=f"User-provided instructions:\n\n{append_text}",
                    cacheable=True,
                )
            )

        return blocks

    def build_system_blocks_for_tool_names(self, tool_names: list[str]) -> list[SystemBlock]:
        """Build system blocks for an arbitrary tool name set (used by subagents).

        Unlike the cached :attr:`system_blocks` property, this always
        recomputes and respects the given *tool_names* so that a
        tool-filtered subagent gets a protocol block matching its actual
        toolset.
        """
        # Temporarily override _cached_system_blocks to avoid polluting the
        # agent-level cache; build with the requested names and return.
        active = set(tool_names)
        registry = self.tools
        if not active.issubset({tool.name for tool in registry.list()}):
            worker_registry = self._subagent_tool_registry
            if worker_registry is not None:
                registry = worker_registry
        tool_sections = registry.system_prompt_sections(active_names=tool_names)
        return self._build_system_blocks(tool_names, tool_sections=tool_sections)

    async def connect_skills(self) -> None:
        if self._skills_loaded:
            return
        if self._skills_connect is not None:
            await self._skills_connect
            return

        async def _load() -> None:
            from .skills.builtins import merge_builtin_skills
            from .skills.listing import build_skill_listing
            from .skills.loader import load_skills_from_dir
            from .tools.skill import SkillTool

            builtin_names = {t.name for t in self.tools.list()}
            # Disk scan + frontmatter parsing is blocking I/O; keep it off the loop.
            disk_skills, _ = await run_blocking(
                load_skills_from_dir, self._config_dir, builtin_names
            )
            loaded = merge_builtin_skills(disk_skills)
            for s in loaded:
                self.skills[s.name] = s

            if self.skills:
                skill_tool = SkillTool(
                    skills=self.skills,
                    session_registry=self._sessions,
                    get_session_model=lambda _sid: self.model,
                )
                self.tools.register(cast("Tool", skill_tool))
                if (
                    self._subagent_tool_registry is not None
                    and self._subagent_tool_registry.get(skill_tool.name) is None
                ):
                    self._subagent_tool_registry.register(cast("Tool", skill_tool))
                self._refresh_system_blocks()

                for s in loaded:
                    if not s.frontmatter.allowed_tools and not s.frontmatter.model:
                        self.permission_engine.rules.append(
                            ToolRule(
                                tool="Skill",
                                decision="allow",
                                arg=s.name,
                            )
                        )

                self.skill_listing_text = (
                    build_skill_listing(
                        skills=loaded,
                        context_window_tokens=self.context_window(),
                    )
                    or None
                )

        self._skills_connect = _load()
        try:
            await self._skills_connect
            self._skills_loaded = True
        except Exception:
            self._skills_connect = None
            raise

    async def connect_subagents(self) -> None:
        if self._subagents_loaded:
            return
        if self._subagents_connect is not None:
            await self._subagents_connect
            return

        async def _load() -> None:
            from .subagents.loader import load_agents_from_dir
            from .subagents.registry import AgentRegistry
            from .tools.subagent import SubagentTool
            from .tools.subagent_continue import SubagentContinueTool
            from .tools.subagent_stop import TaskStopTool

            result = await load_agents_from_dir(self._config_dir)
            registry = AgentRegistry(result.agents, extra_built_ins=self.extra_subagents)
            self.subagent_registry = registry

            get_session = lambda sid: self._sessions.get(sid)  # noqa: E731
            subagent_tool = SubagentTool(
                registry=registry,
                get_session=get_session,
                next_default_display_name=self._next_default_display_name,
                retain_subagents=self.retain_subagents,
                enable_background_subagents=self.enable_background_subagents,
            )
            self.tools.register(cast("Tool", subagent_tool))
            # SubagentTool itself is deliberately excluded from the worker registry:
            # build_child_tools() strips it from every child regardless, so workers
            # cannot recursively spawn their own sub-subagents by default.
            if self.enable_worker_tools:
                continue_tool = SubagentContinueTool(get_session=get_session)
                self.tools.register(cast("Tool", continue_tool))
                if (
                    self._subagent_tool_registry is not None
                    and self._subagent_tool_registry.get(continue_tool.name) is None
                ):
                    self._subagent_tool_registry.register(cast("Tool", continue_tool))
            if self.enable_task_stop:
                stop_tool = TaskStopTool(get_session=get_session)
                self.tools.register(cast("Tool", stop_tool))
                if (
                    self._subagent_tool_registry is not None
                    and self._subagent_tool_registry.get(stop_tool.name) is None
                ):
                    self._subagent_tool_registry.register(cast("Tool", stop_tool))
            self._refresh_system_blocks()

        self._subagents_connect = _load()
        try:
            await self._subagents_connect
            self._subagents_loaded = True
        except Exception:
            self._subagents_connect = None
            raise

    async def reload_subagents(self) -> None:
        """Reload disk-backed subagents and refresh subagent orchestration tools."""

        self.tools.unregister("Subagent")
        self.tools.unregister("SubagentContinue")
        self.tools.unregister("TaskStop")
        self.subagent_registry = None
        self._subagents_connect = None
        self._subagents_loaded = False
        if self.features.subagents:
            await self.connect_subagents()
        else:
            self._refresh_system_blocks()

    def _next_default_display_name(self, session_id: str) -> str:
        cur = self.subagent_run_counters.get(session_id, 0)
        self.subagent_run_counters[session_id] = cur + 1
        return f"Agent #{cur + 1}"

    async def connect_mcp(self) -> None:
        if not self._mcp_servers:
            return
        if self._mcp_connection is not None:
            return
        if self._mcp_connect is not None:
            await self._mcp_connect
            return

        async def _load() -> None:
            from .mcp import connect_mcp_servers

            mcp_conn = await connect_mcp_servers(cast(Any, self._mcp_servers))
            self._attach_mcp_tools(mcp_conn)
            self._mcp_connection = mcp_conn

        self._mcp_connect = _load()
        try:
            await self._mcp_connect
        except Exception:
            self._mcp_connect = None
            raise

    def _attach_mcp_tools(self, connection: Any) -> None:
        """Register a connection's tools + derived permission rules into the live agent.

        Shared by the configured-connect path and mid-run registration. Because
        the per-turn request rebuilds its tool list from ``self.tools`` (no cache),
        tools attached mid-run appear on the next turn. Destructive MCP tools map
        to ``ask`` permission rules via the annotation→permission bridge.
        """
        from .mcp import mcp_permission_rules

        for tool in connection.tools:
            self.tools.register(cast("Tool", tool))
            if (
                self._subagent_tool_registry is not None
                and self._subagent_tool_registry.get(tool.name) is None
            ):
                self._subagent_tool_registry.register(cast("Tool", tool))
        self.permission_engine.rules.extend(mcp_permission_rules(connection.tools))
        self._refresh_system_blocks()

    async def add_mcp_servers(self, servers: dict[str, Any]) -> Any:
        """Connect additional MCP servers during a run; their tools appear next turn.

        Returns the new :class:`McpConnection`, also tracked for ``close()``.
        """
        from .mcp import connect_mcp_servers

        connection = await connect_mcp_servers(cast(Any, servers))
        self._attach_mcp_tools(connection)
        self._extra_mcp_connections.append(connection)
        return connection

    async def session(
        self, id: str | None = None, meta: dict[str, object] | None = None
    ) -> Session:
        from .session import Session

        self._require_open()
        if self._session_lock is None:
            self._session_lock = asyncio.Lock()
        # Session setup is a reservation: same-id callers serialize behind it,
        # and close waits for the reservation to either publish or roll back
        # before shared stores are released.
        async with self._session_lock:
            self._require_open()
            if self.features.mcp:
                await self.connect_mcp()
            if self.features.skills:
                await self.connect_skills()
            if self.features.subagents:
                await self.connect_subagents()

            # One live Session per (agent, id): re-attaching a live id returns the
            # registered instance rather than overwriting it.
            if id is not None:
                existing = self._sessions.get(id)
                if existing is not None and not existing._closed:
                    return existing

            store = self._get_store()
            record = await store.create(id=id, meta=meta)
            messages = await store.load_messages(record.id) if id else []
            full_history = [row.message for row in messages]

            # Optional store capability (Phase 3.3): if a durable compacted snapshot
            # exists, restore that provider_view plus every message appended after the
            # sequence it covers, instead of rebuilding the view from full history.
            provider_view = list(full_history)
            snapshot_loader = getattr(store, "load_provider_snapshot", None)
            if id and snapshot_loader is not None:
                provider_view = await _restore_provider_view(
                    snapshot_loader, record.id, messages, full_history
                )

            session = Session(
                id=record.id,
                created_at=record.created_at,
                meta=record.meta,
                agent=self,
                store=store,
                provider_view=provider_view,
                full_history=full_history,
            )
            # Seed the snapshot watermark from the loaded messages; disable snapshot
            # caching if their seqs are not strictly increasing (can't trust a split).
            _loaded_seqs = [row.seq for row in messages]
            session._last_seq = max(_loaded_seqs, default=0)
            session._seq_cacheable = all(
                b > a for a, b in zip(_loaded_seqs, _loaded_seqs[1:], strict=False)
            )
            # Attach a per-session filesystem backend when the subsystem is active.
            if self._filesystem_active():
                from .filesystem.backend import StateFileBackend

                # If the caller passed a backend, use it as the session-level store.
                # CompositeFileBackend and SqliteFileBackend are shared across sessions
                # by design; StateFileBackend is session-local by default.
                session.filesystem = (
                    self._filesystem_default
                    if self._filesystem_default is not None
                    else StateFileBackend()
                )

            session.invoked_skills = []
            for rec in record.invoked_skills:
                if not isinstance(rec, dict):
                    continue
                session.invoked_skills.append(
                    # Stored metadata may come from JSON backends; normalize defensively.
                    InvokedSkillRecord(
                        name=str(rec.get("name", "")),
                        substituted_body=str(
                            rec.get("substituted_body", rec.get("substitutedBody", ""))
                        ),
                        invoked_at=float(
                            cast(Any, rec.get("invoked_at", rec.get("invokedAt", 0.0)) or 0.0)
                        ),
                    )
                )
            # close() may have started while store/setup calls were awaiting. Do
            # not publish a session into a quiescing agent; mark the unregistered
            # object closed and let close() proceed after this reservation exits.
            if self._lifecycle_state != _OPEN:
                session._lifecycle_state = _CLOSED
                session._closed = True
                raise ConfigError("agent is closing")
            self._sessions[record.id] = session
            return session

    def _require_open(self) -> None:
        if self._lifecycle_state != _OPEN:
            raise ConfigError("agent is closing")

    async def fork_session(
        self,
        source_or_id: Session | str,
        *,
        before_seq: int | None = None,
        id: str | None = None,
        meta: Mapping[str, object] | None = None,
    ) -> Session:
        """Create an independent session from a safe prefix of another.

        ``before_seq`` is an exclusive boundary.  The store-level helper owns
        lineage metadata, assistant/tool pairing validation, and atomic custom
        id handling; this method intentionally remains a thin lifecycle seam.
        """
        from .sessions.fork import fork_session as _fork_session

        return await _fork_session(
            self,
            source_or_id,
            before_seq=before_seq,
            id=id,
            meta=meta,
        )

    async def run_workflow(
        self,
        fn: Any,
        *,
        budget: Any = None,
        run_id: str | None = None,
        max_concurrency: int = 4,
        max_agent_concurrency: int = 0,
        on_event: Any = None,
        step_timeout_ms: float | None = None,
        deadline_ms: float | None = None,
        journal_snapshot_every: int = 0,
        signal: Any = None,
        resume: dict[str, Any] | None = None,
    ) -> Any:
        """Run a deterministic workflow function and return its value.

        *fn* is ``async def flow(wf: WorkflowContext) -> Any``.  See
        :mod:`linch.workflow` for the ``wf`` primitives (``agent``, ``step``,
        ``interrupt``, ``parallel``, ``settled``, ``pipeline``, ``phase``,
        ``budget``) and the journal/resume semantics behind *run_id*.  *resume*
        answers ``wf.interrupt`` calls left pending by an earlier suspend.
        """
        from .workflow.engine import run_workflow as _run_workflow

        return await _run_workflow(
            self,
            fn,
            budget=budget,
            run_id=run_id,
            max_concurrency=max_concurrency,
            max_agent_concurrency=max_agent_concurrency,
            on_event=on_event,
            step_timeout_ms=step_timeout_ms,
            deadline_ms=deadline_ms,
            journal_snapshot_every=journal_snapshot_every,
            signal=signal,
            resume=resume,
        )

    async def release_session(self, session_or_id: Session | str, force: bool = False) -> None:
        """Release a single session's live registration and drain its owned work.

        Idempotent: releasing an unknown or already-released session is a no-op.
        Durable history in the session store is preserved. A session with an
        active run is rejected unless *force* is set.

        Args:
            session_or_id: The session instance or its id.
            force: Whether to abort an active run and drain its work rather than
                raise. See :meth:`Session.aclose`.
        """
        from .session import Session

        if isinstance(session_or_id, Session):
            if session_or_id.agent is not self:
                raise ConfigError("session belongs to a different agent")
            sid = session_or_id.id
            registered = self._sessions.get(sid)
            # A stale/orphaned instance whose id now maps to a different object
            # (or nothing) must not release the replacement — no-op.
            if registered is None or registered is not session_or_id:
                return
            await registered.aclose(force=force)
            return

        sid = str(session_or_id)
        session = self._sessions.get(sid)
        if session is None:
            return
        await session.aclose(force=force)

    def _unregister_session(self, sid: str, session: Session | None = None) -> None:
        """Remove a session from the live registry (does not touch durable history).

        When *session* is given, only unregister if it is the currently-registered
        instance, so a stale object never pops a re-registered replacement.
        """
        if session is not None and self._sessions.get(sid) is not session:
            return
        self._sessions.pop(sid, None)

    async def close(self) -> None:
        """Quiesce sessions, then tear down shared resources transactionally.

        Concurrent callers await one shielded task. Caller cancellation never
        cancels teardown; failures leave the agent quiescing so a later call can
        retry only resources that have not already closed successfully.
        """
        if self._lifecycle_state == _CLOSED:
            return
        # No await precedes this transition: once close() starts executing, all
        # new session/run admission observes QUIESCING immediately.
        self._lifecycle_state = _QUIESCING
        task = self._close_task
        if task is None or task.done():
            task = asyncio.create_task(self._close_impl())
            task.add_done_callback(_consume_task_exception)
            self._close_task = task
        await asyncio.shield(task)

    async def _close_impl(self) -> None:
        import inspect as _inspect

        # Drain and release every live session — cancel owned worker/background
        # tasks and await their finalizers — before closing shared resources, so
        # a finalizer never touches an already-closed store/provider/filesystem.
        if "sessions" not in self._teardown_complete:
            if self._session_lock is None:
                self._session_lock = asyncio.Lock()
            async with self._session_lock:
                for sess in list(self._sessions.values()):
                    await sess.aclose(force=True)
                self._sessions.clear()
            self._teardown_complete.add("sessions")

        if "mcp" not in self._teardown_complete and self._mcp_connection is not None:
            await self._mcp_connection.close()
            self._mcp_connection = None
        self._teardown_complete.add("mcp")
        if "extra_mcp" not in self._teardown_complete:
            while self._extra_mcp_connections:
                connection = self._extra_mcp_connections[0]
                await connection.close()
                self._extra_mcp_connections.pop(0)
            self._teardown_complete.add("extra_mcp")
        if "store" not in self._teardown_complete and self._store is not None:
            await self._store.close()
        self._teardown_complete.add("store")
        if "run_store" not in self._teardown_complete and self.run_store is not None:
            closer = getattr(self.run_store, "close", None)
            if closer is not None:
                result = closer()
                if _inspect.isawaitable(result):
                    await result
        self._teardown_complete.add("run_store")
        if "filesystem" not in self._teardown_complete and self._filesystem_default is not None:
            closer = getattr(self._filesystem_default, "aclose", None) or getattr(
                self._filesystem_default, "close", None
            )
            if closer is not None:
                result = closer()
                if _inspect.isawaitable(result):
                    await result
        self._teardown_complete.add("filesystem")

        # Close the provider's transport (e.g. the OpenAI/httpx connection pool)
        # so closing the agent never leaks sockets/file descriptors. Duck-typed:
        # providers without a closer (test doubles) are skipped. A failing closer
        # leaves the agent quiescing so a later close() can retry it.
        if "provider" not in self._teardown_complete:
            provider_closer = getattr(self._provider, "aclose", None) or getattr(
                self._provider, "close", None
            )
            if provider_closer is not None:
                result = provider_closer()
                if _inspect.isawaitable(result):
                    await result
            self._teardown_complete.add("provider")

        # Close hooks that expose a closer (e.g. RunTelemetryHook flushes its
        # wrapped observers). Successful hooks are remembered across retries.
        # Also close any hooks that were replaced via the hooks setter so their
        # resources (e.g. OTel exporters) are not orphaned.
        for hook in [*self._hooks, *self._replaced_hooks]:
            if id(hook) in self._closed_hook_ids:
                continue
            closer = getattr(hook, "aclose", None) or getattr(hook, "close", None)
            if closer is not None:
                result = closer()
                if _inspect.isawaitable(result):
                    await result
            self._closed_hook_ids.add(id(hook))
        self._teardown_complete.add("hooks")
        self._lifecycle_state = _CLOSED
        self._closed = True


async def _restore_provider_view(
    snapshot_loader: Any,
    sid: str,
    messages: list[Any],
    full_history: list[Message],
) -> list[Message]:
    """Rebuild ``provider_view`` from a durable snapshot, treating it strictly as
    a cache: any load error, an out-of-range watermark, or non-monotonic message
    seqs falls back to the full history (which is always correct)."""
    import logging as _logging

    fallback = list(full_history)
    try:
        snapshot = await snapshot_loader(sid)
    except Exception as exc:
        _logging.getLogger(__name__).warning(
            "provider snapshot load failed; rebuilding from full history: %s", exc
        )
        return fallback
    if snapshot is None:
        return fallback
    max_seq = max((row.seq for row in messages), default=0)
    covers = snapshot.covers_seq
    if not isinstance(covers, int) or covers < 0 or covers > max_seq:
        _logging.getLogger(__name__).warning(
            "provider snapshot watermark %r out of range (0..%d); rebuilding from full history",
            covers,
            max_seq,
        )
        return fallback
    seqs = [row.seq for row in messages]
    if any(b <= a for a, b in zip(seqs, seqs[1:], strict=False)):
        _logging.getLogger(__name__).warning(
            "provider snapshot: message seqs not monotonic; rebuilding from full history"
        )
        return fallback
    tail = [row.message for row in messages if row.seq > covers]
    return list(snapshot.provider_view) + tail


def _consume_task_exception(task: asyncio.Task[None]) -> None:
    """Avoid an un-retrieved exception if a shielded close caller is cancelled."""
    if not task.cancelled():
        task.exception()


def _normalize_permission_mode(raw: object) -> PermissionMode:
    if raw in {"default", "acceptEdits", "skip-dangerous"}:
        return cast(PermissionMode, raw)
    raise ConfigError("permissions.mode must be one of: default, acceptEdits, skip-dangerous")
