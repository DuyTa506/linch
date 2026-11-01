"""Declarative run configuration composed over the existing agent runtime."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from copy import copy, deepcopy
from dataclasses import dataclass, field, fields, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from .errors import ConfigError
from .kernel import Disposable, Plugin
from .run_store import _decode_output_schema, _json_safe, canonical_json

if TYPE_CHECKING:
    from .session import Session


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class Profile:
    """Describe a versioned role, plugin selection, and run defaults.

    Attributes:
        plugins: Frozen ordered rows keyed by stable plugin id.
        tool_allow: Optional offered-tool allowlist; an empty tuple offers none.
        tool_deny: Tool names removed after the allowlist is applied.
        output_schema: Frozen constructor data for structured output.
        budget: Frozen constructor data for per-run spending limits.

    Args:
        id: Stable domain or tenant profile identifier.
        version: Revision selected by the caller, also required on resume.
        system_prompt: Role instructions appended to the agent's instructions.
        plugins: Ordered rows keyed by stable id, each containing a plugin and
            declarative configuration. Live dependencies belong in Context.
        tool_allow: Eligible tool names; an empty tuple permits no tools.
        tool_deny: Tool names removed even when present in the allow list.
        output_schema: Constructor arguments for the existing OutputSchema.
        max_turns: Maximum turns for runs selecting this profile.
        budget: RunBudget limit arguments, instantiated afresh for each run.

    Raises:
        ConfigError: If identity, limits, schema arguments, or plugin identity is invalid.
        TypeError: If configuration cannot be deterministically serialized.
    """

    id: str
    version: str
    system_prompt: str | None = None
    plugins: Mapping[str, tuple[Plugin, Mapping[str, Any]]] = field(default_factory=dict)
    tool_allow: tuple[str, ...] | None = None
    tool_deny: tuple[str, ...] | None = None
    output_schema: Mapping[str, Any] | None = None
    max_turns: int | None = None
    budget: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not value.strip() for value in (self.id, self.version)
        ):
            raise ConfigError("profile id and version must be non-empty strings")
        if self.max_turns is not None and (type(self.max_turns) is not int or self.max_turns < 1):
            raise ConfigError("profile max_turns must be a positive integer")
        if self.system_prompt is not None and not isinstance(self.system_prompt, str):
            raise ConfigError("profile system_prompt must be a string")
        for name in ("tool_allow", "tool_deny"):
            names = getattr(self, name)
            if names is not None and (
                not isinstance(names, list | tuple)
                or any(not isinstance(item, str) or not item.strip() for item in names)
            ):
                raise ConfigError(f"profile {name} must contain non-empty tool names")
        if self.budget is not None:
            if not isinstance(self.budget, Mapping) or any(
                key not in {"max_tokens", "max_cost_usd", "warn_ratio"}
                or (key == "warn_ratio" and value is None)
                or (
                    value is not None
                    and (
                        type(value) not in (int, float)
                        or not math.isfinite(value)
                        or value < 0
                        or (key == "max_tokens" and type(value) is not int)
                        or (key == "warn_ratio" and value > 1)
                    )
                )
                for key, value in self.budget.items()
            ):
                raise ConfigError("profile budget must contain valid RunBudget limits")
        if self.output_schema is not None:
            try:
                _decode_output_schema(
                    _json_safe(
                        {"strict": True, "description": None, **self.output_schema}, strict=True
                    )
                )
            except (TypeError, ValueError) as exc:
                raise ConfigError(f"profile output_schema is invalid: {exc}") from None
        rows = {}
        for key, (plugin, config) in self.plugins.items():
            if any(
                not isinstance(value, str) or not value.strip()
                for value in (key, getattr(plugin, "name", None), getattr(plugin, "version", None))
            ):
                raise ConfigError("profile plugin rows require an id, plugin name and version")
            rows[key] = (plugin, _freeze(json.loads(canonical_json(config))))
        object.__setattr__(self, "plugins", MappingProxyType(rows))
        for name in ("tool_allow", "tool_deny", "output_schema", "budget"):
            object.__setattr__(self, name, _freeze(json.loads(canonical_json(getattr(self, name)))))

    def layer(self, other: Profile) -> Profile:
        """Compose a later layer, replacing supplied fields and plugin rows by id.

        ``None`` inherits a field; plugin rows merge by id. The resulting
        identity comes from the later layer.

        Args:
            other: Domain layer or tenant patch applied after this profile.

        Returns:
            A new profile containing the composed configuration.
        """
        values = {
            item.name: getattr(other, item.name)
            for item in fields(other)
            if getattr(other, item.name) is not None
        }
        values["plugins"] = {**self.plugins, **other.plugins}
        return replace(self, **values)

    def resolve(self) -> dict[str, Any]:
        """Return a detached, JSON-safe view of the composed configuration.

        Returns:
            Effective fields and ordered plugin rows with implementation
            identities and configuration, without live plugin objects.
        """
        values = {item.name: getattr(self, item.name) for item in fields(self)}
        values["plugins"] = {
            key: {"plugin": {"name": plugin.name, "version": plugin.version}, "config": config}
            for key, (plugin, config) in self.plugins.items()
        }
        return _json_safe(values, strict=True)

    def _contract(self) -> dict[str, str]:
        # Plugin order is executable policy even though JSON object keys sort.
        definition = {"resolved": self.resolve(), "plugin_order": list(self.plugins)}
        digest = hashlib.sha256(canonical_json(definition).encode()).hexdigest()
        return {"id": self.id, "version": self.version, "content_hash": f"sha256:{digest}"}


@dataclass(slots=True)
class _RunConfig:
    """Narrow configuration capability with a live base and bounded overlays."""

    _owner: Any
    _values: dict[str, Any] | None = None
    _session: Any = None

    def _read(self, name: str) -> Any:
        if self._values is not None:
            return self._values[name]
        return getattr(self._owner, name)

    def _snapshot(self) -> dict[str, Any]:
        return {
            name: getattr(self, name)
            for name in (
                "context",
                "tools",
                "hooks",
                "permission_engine",
                "tool_pipeline",
                "_turn_stopping_dispatcher",
                "system_prompt",
                "_system_prompt_config",
                "_cached_system_blocks",
                "_cached_system_blocks_signature",
                "max_turns",
                "output_schema",
                "budget",
            )
        }

    @property
    def context(self) -> Any:
        return self._read("context")

    @property
    def tools(self) -> Any:
        if self._values is None and self._session is not None:
            override = self._session.tools_override
            if override is not None:
                return override
        return self._read("tools")

    @property
    def hooks(self) -> Any:
        return self._read("hooks")

    @property
    def permission_engine(self) -> Any:
        return self._read("permission_engine")

    @property
    def tool_pipeline(self) -> Any:
        return self._read("tool_pipeline")

    @property
    def _turn_stopping_dispatcher(self) -> Any:
        return self._read("_turn_stopping_dispatcher")

    @property
    def system_prompt(self) -> Any:
        return self._read("system_prompt")

    @property
    def _system_prompt_config(self) -> Any:
        return self._read("_system_prompt_config")

    @property
    def _cached_system_blocks(self) -> Any:
        return self._read("_cached_system_blocks")

    @property
    def _cached_system_blocks_signature(self) -> Any:
        return self._read("_cached_system_blocks_signature")

    @property
    def max_turns(self) -> Any:
        return self._read("max_turns")

    @property
    def output_schema(self) -> Any:
        return self._read("output_schema")

    @property
    def budget(self) -> Any:
        return self._read("budget")

    @property
    def system_blocks_override(self) -> Any:
        if self._values is not None:
            return self._values.get("system_blocks_override")
        return self._session.system_blocks_override if self._session is not None else None

    def inbox(self, producer: Any) -> Disposable:
        """Register a producer in this run configuration's delivery bucket.

        Args:
            producer: Async iterator factory called with the session and run id.

        Returns:
            A disposer to be owned by the plugin's activation ``ctx.effect``.
        """
        self.context._scope.ensure_active()
        bucket = self.context._inbox
        if bucket is None:
            bucket = self.context._inbox = []
        entry = (object(), producer)
        bucket.append(entry)
        return Disposable(lambda: bucket.remove(entry))

    def build_system_blocks_for_tool_names(self, tool_names: list[str]) -> list[Any]:
        """Build prompt blocks from this configuration's admitted tool catalog.

        Args:
            tool_names: Admitted names whose protocol and contributions are included.

        Returns:
            Fresh prompt blocks without changing the owning Agent cache.
        """
        registry = self.tools
        if self._values is None and not set(tool_names).issubset(
            {tool.name for tool in registry.list()}
        ):
            worker_registry = self._owner._subagent_tool_registry
            if worker_registry is not None:
                registry = worker_registry
        return self._build_system_blocks(
            tool_names, tool_sections=registry.system_prompt_sections(active_names=tool_names)
        )

    def _build_system_blocks(
        self, tool_names: list[str], *, tool_sections: list[Any] | None = None
    ) -> list[Any]:
        from .agent import _build_system_blocks_for

        return _build_system_blocks_for(
            tool_names=tool_names,
            tool_sections=tool_sections,
            cwd=self._owner.cwd,
            bash_security=self._owner._bash_security_mode(),
            permission_mode=self.permission_engine.mode,
            system_prompt=self.system_prompt,
            system_prompt_config=self._system_prompt_config,
        )

    @property
    def system_blocks(self) -> list[Any]:
        from .agent import _system_blocks_signature_for

        if self._values is None:
            if self._session is not None and self._session.tools_override is not None:
                return self.build_system_blocks_for_tool_names(
                    sorted(tool.name for tool in self.tools.list())
                )
            return self._owner.system_blocks
        names = sorted(tool.name for tool in self.tools.list())
        signature = _system_blocks_signature_for(
            tools=self.tools,
            tool_names=names,
            tool_sections=self.tools.system_prompt_sections(active_names=names),
            system_prompt=self.system_prompt,
            system_prompt_config=self._system_prompt_config,
            permission_mode=self.permission_engine.mode,
            bash_security=self._owner._bash_security_mode(),
        )
        if self._cached_system_blocks_signature != signature:
            self._values["_cached_system_blocks"] = self.build_system_blocks_for_tool_names(names)
            self._values["_cached_system_blocks_signature"] = signature
        return self._cached_system_blocks


@dataclass(slots=True)
class _RunBinding:
    session: Any
    run_id: str
    config: _RunConfig
    parent: _RunBinding | None = None
    context: Any = None
    retains: int = 1
    cleanup: Disposable | None = None
    profile: Profile | None = None
    budget: Any = None

    def retain(self) -> _RunBinding:
        if self.retains < 1:
            raise ConfigError("run configuration is already released")
        self.retains += 1
        return self

    async def release(self) -> None:
        if self.retains:
            self.retains -= 1
        if self.retains:
            return
        if self.cleanup is not None:
            await self.cleanup.dispose()
        else:
            self.session._run_bindings.pop(self.run_id, None)


def _base_run_config(session: Session) -> _RunConfig:
    """Resolve live owner fields and Session overrides without an admitted run."""
    return _RunConfig(session.agent, _session=session)


def _effective_run_config(session: Session, run_id: str) -> _RunConfig:
    binding = session._run_bindings.get(run_id)
    if binding is None or binding.config._owner is not session.agent:
        raise ConfigError(f"missing or foreign run configuration binding: {run_id!r}")
    return binding.config


@asynccontextmanager
async def _profile_scope(
    session: Session, profile: Profile | None, run_id: str
) -> AsyncIterator[_RunConfig]:
    from .budget import RunBudget
    from .kernel.plugins import _PluginMount
    from .loop.turn_stopping import TurnStoppingDispatcher
    from .permissions import PermissionLayer, PermissionRuleSet, ToolRule
    from .types import OutputSchema

    existing = session._run_bindings.get(run_id)
    if existing is not None and existing.retains == 0:
        await existing.release()
        existing = None
    if existing is not None:
        existing.retain()
        try:
            yield existing.config
        finally:
            await existing.release()
        return
    parent = session.agent
    seed = session._config_seed
    base = seed.config if seed is not None else parent._run_config
    context = None
    if profile is not None:
        if not isinstance(profile, Profile):
            raise ConfigError("run profile must be a Profile")
        await parent._ensure_provider_prepared()
        context = base.context.scope(f"profile:{profile.id}@{profile.version}", isolate_events=True)
        values = base._snapshot()
        values.update(
            context=context,
            tools=(
                session.tools_override if session.tools_override is not None else base.tools
            ).copy(),
            hooks=list(base.hooks or ()),
            permission_engine=copy(base.permission_engine),
            tool_pipeline=base.tool_pipeline.fork(),
            _turn_stopping_dispatcher=TurnStoppingDispatcher(
                base._turn_stopping_dispatcher.snapshot()
            ),
            _cached_system_blocks=None,
            _cached_system_blocks_signature=None,
            system_blocks_override=session.system_blocks_override,
        )
        values["permission_engine"].rules = deepcopy(base.permission_engine.rules)
        values["permission_engine"].rule_set = deepcopy(base.permission_engine.rule_set)
        config = _RunConfig(parent, values)
        context.register("run_config", config)
        context.register("tool_pipeline", config.tool_pipeline)
    else:
        config = base if seed is not None else _base_run_config(session)
        if seed is not None and session.tools_override is not None:
            config = _RunConfig(
                parent,
                {
                    **base._snapshot(),
                    "tools": session.tools_override,
                    "system_blocks_override": session.system_blocks_override,
                },
            )
    binding = _RunBinding(session, run_id, config, parent=seed, context=context, profile=profile)
    if seed is not None:
        seed.retain()
    session._run_bindings[run_id] = binding

    async def cleanup() -> None:
        if context is not None:
            await context.dispose()
        if seed is not None:
            await seed.release()
        session._run_bindings.pop(run_id, None)

    binding.cleanup = base.context.effect(lambda: Disposable(cleanup))
    try:
        if profile is not None:
            assert context is not None
            assert config._values is not None
            for plugin, plugin_config in profile.plugins.values():
                await _PluginMount(
                    context, plugin, json.loads(canonical_json(plugin_config))
                ).start()
            eligible = set(profile.tool_allow) if profile.tool_allow is not None else None
            if session.tools_override is not None:
                inherited = {tool.name for tool in session.tools_override.list()}
                eligible = inherited if eligible is None else inherited & eligible
            restricted = config.tools.subset(include=eligible, exclude=set(profile.tool_deny or ()))
            denied = {tool.name for tool in config.tools.list()} - {
                t.name for t in restricted.list()
            }
            if denied:
                engine = config.permission_engine
                inherited_rules = engine.rule_set
                engine.rule_set = PermissionRuleSet(
                    [
                        *getattr(inherited_rules, "layers", ()),
                        PermissionLayer(
                            "profile", [ToolRule(name, "deny") for name in sorted(denied)]
                        ),
                    ],
                    project_root=getattr(inherited_rules, "project_root", engine.project_root),
                )
            config._values["tools"] = restricted
            if profile.system_prompt is not None:
                if session.system_blocks_override is not None:
                    from .types import SystemBlock

                    config._values["system_blocks_override"] = [
                        *session.system_blocks_override,
                        SystemBlock(text=profile.system_prompt, cacheable=True),
                    ]
                cfg = config._system_prompt_config
                prompt = (
                    cfg.append
                    if cfg is not None and cfg.append is not None
                    else config.system_prompt
                )
                prompt = "\n\n".join(part for part in (prompt, profile.system_prompt) if part)
                config._values["system_prompt"] = prompt
                if cfg is not None:
                    config._values["_system_prompt_config"] = replace(cfg, append=prompt)
            if profile.max_turns is not None:
                config._values["max_turns"] = profile.max_turns
            resolved = profile.resolve()
            if profile.output_schema is not None:
                config._values["output_schema"] = OutputSchema(**resolved["output_schema"])
            if profile.budget is not None:
                config._values["budget"] = RunBudget(**resolved["budget"])
        yield config
    finally:
        await binding.release()


def _schedule_with_binding(binding: _RunBinding | None, work: Any) -> asyncio.Task[Any]:
    """Retain before scheduling, including cancellation before the first step."""
    from .session import _consume_task_exception

    if binding is not None:
        binding.retain()
    started = False

    async def run() -> Any:
        nonlocal started
        started = True
        try:
            return await work
        finally:
            if binding is not None:
                await binding.release()

    def completed(task: asyncio.Task[Any]) -> None:
        if started:
            return
        work.close()
        if binding is not None:
            binding.retains -= 1
            if binding.retains == 0:
                cleanup = asyncio.create_task(binding.release())
                cleanup.add_done_callback(_consume_task_exception)

    task = asyncio.create_task(run())
    task.add_done_callback(completed)
    return task
