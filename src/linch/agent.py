from __future__ import annotations

import os
import platform
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from ._version import get_version
from .config import FeatureFlags, SystemPromptConfig
from .errors import ConfigError
from .openai_responses import OpenAIOptions, OpenAIReasoning
from .permissions import BashRule, CanUseTool, PathRule, PermissionEngine, PermissionRule, ToolRule
from .providers import BaseProvider, OpenAIResponsesProvider, OpenAIResponsesProviderOptions
from .sessions import SessionStore, SqliteSessionStore
from .tools import ToolRegistry, default_tools
from .types import InvokedSkillRecord, PermissionMode, SystemBlock

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


def _system_prompt_section_blocks(cfg: SystemPromptConfig | None) -> dict[str, list[SystemBlock]]:
    grouped: dict[str, list[SystemBlock]] = {placement: [] for placement in _SECTION_PLACEMENTS}
    if cfg is None or not cfg.sections:
        return grouped

    for section in cfg.sections:
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
    cwd: str | None = None
    system_prompt: str | None = None
    system_prompt_config: SystemPromptConfig | None = None
    max_retries: int = 5
    max_output_tokens: int | None = None
    include_partial_messages: bool = False
    max_turns: int | None = None
    max_tool_concurrency: int | None = None
    tool_timeout_ms: float | None = None
    tool_retry: Any = None  # RetryOptions | None
    cache_ttl: str | None = None
    config_dir: str | None = None
    mcp_servers: dict[str, Any] | None = None
    compaction: Any = None
    compaction_ladder: Any = None  # CompactionLadder | None
    token_estimator: Any = None
    budget: Any = None  # RunBudget | None
    features: FeatureFlags | None = None
    deps: Any = None
    output_schema: Any = None  # OutputSchema | None
    tool_choice: Any = None  # ToolChoice | None
    final_tool_name: str | None = None
    loop_guard: Any = None  # LoopGuard | None; None means "use default LoopGuard"
    filesystem: Any = None  # FileBackend | None
    result_offload: Any = None  # OffloadConfig | None; None = use default OffloadConfig()
    hooks: Any = None
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
        hooks: Any = None,
        extra_subagents: list[AgentDefinition] | None = None,
        enable_worker_tools: bool = False,
        retain_subagents: bool = False,
        enable_background_subagents: bool = False,
        enable_task_stop: bool = False,
        execution_backend: Any = None,
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
        self.tools = tools or default_tools()
        self.execution_backend = execution_backend
        if execution_backend is not None:
            from .tools.builtin import BashTool

            if self.tools.get("Bash") is not None:
                self.tools.replace(BashTool(backend=execution_backend))
        self.permission_engine = PermissionEngine(
            mode=permissions_config.mode,
            rules=permissions_config.rules,
            can_use_tool=permissions_config.can_use_tool,
            project_root=cwd_resolved,
        )
        self._store: SessionStore | None = session_store
        self.run_store: RunStore | None = run_store
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
        self.compaction: Any = compaction
        # Opt-in micro/reactive compaction rungs (CompactionLadder | None).
        # None keeps the legacy single-retry behavior byte-identical.
        self.compaction_ladder: Any = compaction_ladder
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
        self._configure_loop_guard(loop_guard, loopGuard)
        self._configure_hooks(hooks=hooks)
        self._configure_filesystem(filesystem, result_offload)

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
        self._mcp_connect: Any = None
        self._mcp_connection: Any = None

    def _configure_loop_guard(self, loop_guard: Any, loop_guard_alias: Any) -> None:
        from .loop_guard import LoopGuard as _LoopGuard
        from .loop_guard import normalize_loop_guard as _normalize_loop_guard

        effective_lg = loop_guard_alias if loop_guard_alias is not _UNSET else loop_guard
        if effective_lg is _UNSET:
            self.loop_guard: _LoopGuard | None = _LoopGuard()
        else:
            self.loop_guard = _normalize_loop_guard(effective_lg)

    def _configure_hooks(self, *, hooks: Any) -> None:
        from .hooks import normalize_hooks

        self._hooks: list[Any] = normalize_hooks(hooks)
        # Accumulates hooks removed by the hooks setter so Agent.close() can
        # still call their close/aclose methods and avoid resource leaks.
        self._replaced_hooks: list[Any] = []

    def _configure_filesystem(self, filesystem: Any, result_offload: Any) -> None:
        self._filesystem_default: Any = filesystem
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

    def _filesystem_active(self) -> bool:
        """Return True when the virtual filesystem subsystem should be on."""
        enabled = getattr(self.features, "filesystem", True)
        return bool(enabled) and (
            self._filesystem_default is not None or self.result_offload is not None
        )

    @property
    def system_blocks(self) -> list[SystemBlock]:
        if self._cached_system_blocks is not None:
            return self._cached_system_blocks
        tool_names = sorted(tool.name for tool in self.tools.list())
        blocks = self._build_system_blocks(tool_names)
        self._cached_system_blocks = blocks
        return blocks

    def _refresh_system_blocks(self) -> None:
        self._cached_system_blocks = None

    def _get_store(self) -> SessionStore:
        if self._store is None:
            self._store = SqliteSessionStore(Path(self.cwd) / ".linch" / "sessions.db")
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

    @property
    def provider(self) -> BaseProvider:
        return self._provider

    @provider.setter
    def provider(self, value: BaseProvider) -> None:
        self._provider = value

    @property
    def openai(self) -> BaseProvider:
        # Backward compatibility for older integrations.
        return self._provider

    @openai.setter
    def openai(self, value: BaseProvider) -> None:
        self._provider = value

    def _build_system_blocks(self, tool_names: list[str]) -> list[SystemBlock]:
        names = ", ".join(sorted(tool_names))
        shell = os.environ.get("SHELL", os.environ.get("COMSPEC", "unknown"))
        py_ver = platform.python_version()
        os_info = f"{platform.system()} {platform.release()} ({platform.machine()})"

        # ── SWE identity block ───────────────────────────────────────────────
        _SWE_IDENTITY = (
            "You are Linch, an autonomous software engineering assistant. "
            "You work inside a codebase on the user's computer, using the "
            "file and shell tools provided to read, modify, and run code. "
            "Your goal is to complete the user's coding task correctly and "
            "minimally.\n\n"
            "Behave like an experienced engineer: read before you write, run "
            "before you claim, prefer small focused changes, surface "
            "uncertainty instead of guessing. Do not narrate trivial "
            "operations. Do not produce status reports unless the user asked."
        )

        # ── Tool-aware protocol block ────────────────────────────────────────
        # Only include clauses for the tools that are actually present so
        # non-SWE agents (RAG, SQL, …) aren't given misleading instructions.
        present = set(tool_names)
        swe_tool_families = {
            "Read",
            "Edit",
            "Write",
            "Glob",
            "Grep",
            "Bash",
        }
        has_swe_tools = bool(present & swe_tool_families)
        protocol_lines: list[str] = []
        if {"Read", "Edit"} & present:
            protocol_lines.append(
                "- Read a file before you Edit it. The Edit tool will refuse if you have not."
            )
            protocol_lines.append(
                "- Edits require an exact byte-for-byte match of the old_string "
                "in the current file contents, including indentation."
            )
        if {"Write", "Edit"} & present:
            protocol_lines.append(
                "- Prefer Edit over Write when modifying an existing file. Use "
                "Write only for new files or full rewrites."
            )
        if {"Glob", "Grep"} & present:
            protocol_lines.append(
                "- Glob is for finding files by name pattern; Grep is for "
                "searching file contents. They are read-only."
            )
        if "Bash" in present:
            if self.execution_backend is None:
                protocol_lines.append(
                    "- Bash runs in the user's environment with full permissions. "
                    "There is no sandbox. Avoid commands that change global state "
                    "unless the user asked for them."
                )
            else:
                protocol_lines.append(
                    "- Bash runs inside a sandbox. Commands are isolated from the host environment."
                )
        # Always include the generic parallel-tool hint when any tools exist.
        if present:
            if has_swe_tools:
                protocol_lines.append(
                    "- Issue multiple tool calls in a single turn when they are "
                    "independent. Read, Glob, and Grep can run concurrently."
                )
            else:
                protocol_lines.append(
                    "- Issue multiple tool calls in a single turn when they are independent."
                )

        # ── env block (always included) ──────────────────────────────────────
        env_text = (
            f"Environment:\n\n"
            f"- Linch version: {get_version()}\n"
            f"- Working directory: {self.cwd}\n"
            f"- OS: {os_info}\n"
            f"- Shell: {shell}\n"
            f"- Python: {py_ver}\n"
            f"- Permission mode: {self.permission_engine.mode}\n"
            f"- Tools available: {names}"
        )

        cfg = self._system_prompt_config

        # ── Assemble blocks ──────────────────────────────────────────────────
        blocks: list[SystemBlock] = []
        section_blocks = _system_prompt_section_blocks(cfg)

        if cfg is not None and cfg.replace_defaults:
            # Custom-identity / non-SWE mode: skip built-in identity + protocol
            blocks.extend(section_blocks["before_defaults"])
            if cfg.blocks:
                blocks.extend(cfg.blocks)
            blocks.extend(section_blocks["after_defaults"])
        else:
            # Default SWE mode: prepend any extra blocks, then identity + protocol
            blocks.extend(section_blocks["before_defaults"])
            if cfg is not None and cfg.blocks:
                blocks.extend(cfg.blocks)
            blocks.append(SystemBlock(text=_SWE_IDENTITY, cacheable=True))
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
        saved = self._cached_system_blocks
        # Stash current system_prompt_config; use same one (child inherits parent config)
        result = self._build_system_blocks(tool_names)
        self._cached_system_blocks = saved
        return result

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
            disk_skills, _ = load_skills_from_dir(self._config_dir, builtin_names)
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
            if self.enable_worker_tools:
                self.tools.register(cast("Tool", SubagentContinueTool(get_session=get_session)))
            if self.enable_task_stop:
                self.tools.register(cast("Tool", TaskStopTool(get_session=get_session)))
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
            for tool in mcp_conn.tools:
                self.tools.register(cast("Tool", tool))
            self._mcp_connection = mcp_conn
            self._refresh_system_blocks()

        self._mcp_connect = _load()
        try:
            await self._mcp_connect
        except Exception:
            self._mcp_connect = None
            raise

    async def session(
        self, id: str | None = None, meta: dict[str, object] | None = None
    ) -> Session:
        from .session import Session

        if self.features.mcp:
            await self.connect_mcp()
        if self.features.skills:
            await self.connect_skills()
        if self.features.subagents:
            await self.connect_subagents()

        store = self._get_store()
        record = await store.create(id=id, meta=meta)
        messages = await store.load_messages(record.id) if id else []

        session = Session(
            id=record.id,
            created_at=record.created_at,
            meta=record.meta,
            agent=self,
            store=store,
            provider_view=[row.message for row in messages],
            full_history=[row.message for row in messages],
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
        self._sessions[record.id] = session
        return session

    async def run_workflow(
        self,
        fn: Any,
        *,
        budget: Any = None,
        run_id: str | None = None,
        max_concurrency: int = 4,
        on_event: Any = None,
    ) -> Any:
        """Run a deterministic workflow function and return its value.

        *fn* is ``async def flow(wf: WorkflowContext) -> Any``.  See
        :mod:`linch.workflow` for the ``wf`` primitives (``agent``,
        ``parallel``, ``pipeline``, ``phase``, ``budget``) and the
        journal/resume semantics behind *run_id*.
        """
        from .workflow.engine import run_workflow as _run_workflow

        return await _run_workflow(
            self,
            fn,
            budget=budget,
            run_id=run_id,
            max_concurrency=max_concurrency,
            on_event=on_event,
        )

    async def close(self) -> None:
        import asyncio
        import inspect as _inspect

        # Cancel background worker tasks and release retained child sessions.
        for sess in list(self._sessions.values()):
            for handle in getattr(sess, "workers", {}).values():
                task = getattr(handle, "task", None)
                if task is not None and isinstance(task, asyncio.Task) and not task.done():
                    task.cancel()
        self._sessions.clear()

        if self._mcp_connection is not None:
            await self._mcp_connection.close()
            self._mcp_connection = None
        if self._store is not None:
            await self._store.close()
        if self.run_store is not None:
            closer = getattr(self.run_store, "close", None)
            if closer is not None:
                result = closer()
                if _inspect.isawaitable(result):
                    await result
        if self._filesystem_default is not None:
            closer = getattr(self._filesystem_default, "aclose", None) or getattr(
                self._filesystem_default, "close", None
            )
            if closer is not None:
                result = closer()
                if _inspect.isawaitable(result):
                    await result

        # Close hooks that expose a closer (e.g. RunTelemetryHook flushes its
        # wrapped observers).  A faulty closer never aborts agent shutdown.
        # Also close any hooks that were replaced via the hooks setter so their
        # resources (e.g. OTel exporters) are not orphaned.
        for hook in [*self._hooks, *self._replaced_hooks]:
            closer = getattr(hook, "aclose", None) or getattr(hook, "close", None)
            if closer is None:
                continue
            try:
                result = closer()
                if _inspect.isawaitable(result):
                    await result
            except Exception:
                pass


def _normalize_permission_mode(raw: object) -> PermissionMode:
    if raw in {"default", "acceptEdits", "skip-dangerous"}:
        return cast(PermissionMode, raw)
    raise ConfigError("permissions.mode must be one of: default, acceptEdits, skip-dangerous")
