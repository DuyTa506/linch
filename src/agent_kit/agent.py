from __future__ import annotations

import os
import platform
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import FeatureFlags, SystemPromptConfig
from .errors import ConfigError
from .openai_responses import OpenAIOptions, OpenAIReasoning
from .permissions import BashRule, PathRule, PermissionEngine, ToolRule
from .providers import BaseProvider, OpenAIResponsesProvider, OpenAIResponsesProviderOptions
from .sessions import SessionStore, SqliteSessionStore
from .tools import ToolRegistry, default_tools
from .types import InvokedSkillRecord, PermissionMode, SystemBlock

if TYPE_CHECKING:
    from .context_hooks import ContextInjector
    from .session import Session
    from .types import OutputSchema, ToolChoice


@dataclass(slots=True)
class AgentOptions:
    model: str
    provider: BaseProvider | None = None
    openai: OpenAIOptions = field(default_factory=OpenAIOptions)
    reasoning: OpenAIReasoning | None = None
    tools: ToolRegistry | None = None
    permissions: dict[str, object] | None = None
    session_store: SessionStore | None = None
    cwd: str | None = None
    system_prompt: str | None = None
    system_prompt_config: SystemPromptConfig | None = None
    max_retries: int = 5
    max_output_tokens: int | None = None
    include_partial_messages: bool = False
    max_turns: int | None = None
    cache_ttl: str | None = None
    config_dir: str | None = None
    mcp_servers: dict[str, Any] | None = None
    compaction: Any = None
    token_estimator: Any = None
    features: FeatureFlags | None = None
    context_injectors: list[Any] | None = None
    deps: Any = None
    output_schema: Any = None  # OutputSchema | None
    tool_choice: Any = None  # ToolChoice | None
    final_tool_name: str | None = None


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
        cache_ttl: str | None = None,
        cacheTtl: str | None = None,
        config_dir: str | None = None,
        configDir: str | None = None,
        mcp_servers: dict[str, Any] | None = None,
        mcpServers: dict[str, Any] | None = None,
        compaction: Any = None,
        token_estimator: Any = None,
        features: FeatureFlags | None = None,
        context_injectors: list[ContextInjector] | None = None,
        deps: Any = None,
        output_schema: OutputSchema | None = None,
        tool_choice: ToolChoice | None = None,
        final_tool_name: str | None = None,
    ) -> None:
        if systemPrompt is not None:
            system_prompt = systemPrompt
        # SystemPromptConfig takes precedence over the bare system_prompt string;
        # if both are provided the config wins (its .append field overrides).
        if system_prompt_config is not None and system_prompt_config.append is not None:
            system_prompt = system_prompt_config.append
        elif system_prompt_config is not None:
            # replace_defaults or custom blocks but no append text — keep
            # system_prompt as-is (may be None).
            pass
        if maxRetries is not None:
            max_retries = maxRetries
        if maxOutputTokens is not None:
            max_output_tokens = maxOutputTokens
        if includePartialMessages is not None:
            include_partial_messages = includePartialMessages
        if maxTurns is not None:
            max_turns = maxTurns
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

        if openai is None:
            openai = OpenAIOptions(api_key=openai_api_key, base_url=openai_base_url)
        if permissions is None:
            perm_mode: PermissionMode = "default"
            perm_rules = []
            perm_can_use = None
        elif isinstance(permissions, dict):
            perm_mode = _normalize_permission_mode(permissions.get("mode", "default"))
            rules_raw = permissions.get("rules", [])
            if not isinstance(rules_raw, list):
                raise ConfigError("permissions.rules must be a list")
            perm_rules = []
            for rule in rules_raw:
                if not isinstance(rule, (ToolRule, PathRule, BashRule)):
                    raise ConfigError(
                        "permissions.rules entries must be ToolRule, PathRule, or BashRule"
                    )
                perm_rules.append(rule)
            perm_can_use = permissions.get("canUseTool") or permissions.get("can_use_tool")
        else:
            perm_mode = _normalize_permission_mode(getattr(permissions, "mode", "default"))
            perm_rules = list(permissions.rules) if permissions.rules else []
            perm_can_use = getattr(permissions, "canUseTool", None) or getattr(
                permissions, "can_use_tool", None
            )

        cwd_resolved = str(Path(cwd or os.getcwd()).resolve())
        self.model = model
        self.cwd = cwd_resolved
        self.tools = tools or default_tools()
        self.permission_engine = PermissionEngine(
            mode=perm_mode,
            rules=perm_rules,
            can_use_tool=perm_can_use,
            project_root=cwd_resolved,
        )
        self._store: SessionStore | None = session_store
        self.system_prompt = system_prompt
        self.max_retries = max_retries
        self.max_output_tokens = max_output_tokens
        self.include_partial_messages = include_partial_messages
        self.max_turns = max_turns or float("inf")
        if provider is None:
            if openai is None:
                openai = OpenAIOptions(api_key=openai_api_key, base_url=openai_base_url)
            provider = OpenAIResponsesProvider(
                OpenAIResponsesProviderOptions(
                    api_key=openai.api_key,
                    base_url=openai.base_url,
                    default_headers=openai.default_headers,
                    reasoning=reasoning,
                )
            )
        self._provider = provider
        self.cache_ttl = cache_ttl
        self._config_dir = str(Path(cwd_resolved) / (config_dir or ".agent_kit"))
        self._mcp_servers = mcp_servers
        env_concurrency = os.getenv("AGENTKIT_MAX_TOOL_CONCURRENCY")
        self.tool_concurrency = (
            int(env_concurrency)
            if isinstance(env_concurrency, str) and env_concurrency.isdigit()
            else (os.cpu_count() or 4)
        )

        self.skills: dict[str, Any] = {}
        self.skill_listing_text: str | None = None
        self._sessions: dict[str, Session] = {}
        self.subagent_registry: Any = None
        self.subagent_run_counters: dict[str, int] = {}
        self.compaction: Any = compaction
        self.token_estimator = token_estimator

        # Feature flags (controls which subsystems connect in session())
        self.features: FeatureFlags = features or FeatureFlags()

        # Per-turn context injection hooks
        self.context_injectors: list[ContextInjector] = list(context_injectors or [])

        # App-state dependency object threaded into ToolContext.deps
        self.deps: Any = deps

        # Output contracting defaults (can be overridden per-run via RunOptions)
        self.output_schema: OutputSchema | None = output_schema
        self.tool_choice: ToolChoice | None = tool_choice
        self.final_tool_name: str | None = final_tool_name

        # Store SystemPromptConfig for use in _build_system_blocks
        self._system_prompt_config: SystemPromptConfig | None = system_prompt_config

        self._skills_connect: Any = None
        self._subagents_connect: Any = None
        self._mcp_connect: Any = None
        self._mcp_connection: Any = None

        self._cached_system_blocks: list[SystemBlock] | None = None

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
            self._store = SqliteSessionStore(Path(self.cwd) / ".agent_kit" / "sessions.db")
        return self._store

    @property
    def session_store(self) -> SessionStore | None:
        return self._store

    @session_store.setter
    def session_store(self, value: SessionStore | None) -> None:
        self._store = value

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
            "You are AgentKit, an autonomous software engineering assistant. "
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
            protocol_lines.append(
                "- Bash runs in the user's environment with full permissions. "
                "There is no sandbox. Avoid commands that change global state "
                "unless the user asked for them."
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
            f"- AgentKit version: 0.1.0\n"
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

        if cfg is not None and cfg.replace_defaults:
            # Custom-identity / non-SWE mode: skip built-in identity + protocol
            if cfg.blocks:
                blocks.extend(cfg.blocks)
        else:
            # Default SWE mode: prepend any extra blocks, then identity + protocol
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

        # env_text is always present
        blocks.append(SystemBlock(text=env_text, cacheable=True))

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
        if self._skills_connect is not None:
            await self._skills_connect
            return

        async def _load() -> None:
            from .skills.listing import build_skill_listing
            from .skills.loader import load_skills_from_dir
            from .tools.skill import SkillTool

            builtin_names = {t.name for t in self.tools.list()}
            loaded, _ = load_skills_from_dir(self._config_dir, builtin_names)
            for s in loaded:
                self.skills[s.name] = s

            if self.skills:
                skill_tool = SkillTool(
                    skills=self.skills,
                    session_registry=self._sessions,
                    get_session_model=lambda _sid: self.model,
                )
                self.tools.register(skill_tool)
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
        except Exception:
            self._skills_connect = None
            raise

    async def connect_subagents(self) -> None:
        if self._subagents_connect is not None:
            await self._subagents_connect
            return

        async def _load() -> None:
            from .subagents.loader import load_agents_from_dir
            from .subagents.registry import AgentRegistry
            from .tools.subagent import SubagentTool

            result = await load_agents_from_dir(self._config_dir)
            registry = AgentRegistry(result.agents)
            self.subagent_registry = registry

            subagent_tool = SubagentTool(
                registry=registry,
                get_session=lambda sid: self._sessions.get(sid),
                next_default_display_name=self._next_default_display_name,
            )
            self.tools.register(subagent_tool)
            self._refresh_system_blocks()

        self._subagents_connect = _load()
        try:
            await self._subagents_connect
        except Exception:
            self._subagents_connect = None
            raise

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

            mcp_conn = await connect_mcp_servers(self._mcp_servers)
            for tool in mcp_conn.tools:
                self.tools.register(tool)
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
        session.invoked_skills = []
        for rec in record.invoked_skills:
            if not isinstance(rec, dict):
                continue
            session.invoked_skills.append(
                InvokedSkillRecord(
                    name=str(rec.get("name", "")),
                    substituted_body=str(
                        rec.get("substituted_body", rec.get("substitutedBody", ""))
                    ),
                    invoked_at=float(rec.get("invoked_at", rec.get("invokedAt", 0.0)) or 0.0),
                )
            )
        self._sessions[record.id] = session
        return session

    async def close(self) -> None:
        if self._mcp_connection is not None:
            await self._mcp_connection.close()
            self._mcp_connection = None
        if self._store is not None:
            await self._store.close()


def _normalize_permission_mode(raw: object) -> PermissionMode:
    if raw in {"default", "acceptEdits", "skip-dangerous"}:
        return raw
    raise ConfigError("permissions.mode must be one of: default, acceptEdits, yolo")
