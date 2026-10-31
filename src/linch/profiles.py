"""Declarative run configuration composed over the existing agent runtime."""

from __future__ import annotations

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
from .kernel import EventBus, Plugin
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
            item.name: (
                getattr(self, item.name)
                if getattr(other, item.name) is None
                else getattr(other, item.name)
            )
            for item in fields(self)
        }
        values["plugins"] = {**self.plugins, **other.plugins}
        return Profile(**values)

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


def _copy_bus(original: EventBus, *, skip_services: bool = False) -> EventBus:
    bus = EventBus()
    for name, listeners in original._listeners.items():
        if skip_services and name == "context/service":
            continue
        for listener in listeners:
            bus.on(name, listener)
    return bus


@asynccontextmanager
async def _profile_scope(session: Session, profile: Profile) -> AsyncIterator[None]:
    from .budget import RunBudget
    from .loop.turn_stopping import TurnStoppingDispatcher
    from .permissions import PermissionLayer, PermissionRuleSet, ToolRule
    from .types import OutputSchema, SystemBlock

    if not isinstance(profile, Profile):
        raise ConfigError("run profile must be a Profile")
    parent = session.agent
    await parent._ensure_provider_prepared()
    agent = copy(parent)
    context = parent.context.scope(f"profile:{profile.id}@{profile.version}")
    context.events = _copy_bus(parent.context.events, skip_services=True)
    agent.context = context
    original_tools, original_system = session.tools_override, session.system_blocks_override
    try:

        async def service_changed(changed: Any, key: str) -> None:
            if context._within(changed) and context.events.has_listeners("context/service"):
                await context.events._notification("context/service", changed, key).dispose()

        context.effect(lambda: parent.context.events.on("context/service", service_changed))
        # Copy configuration surfaces; provider, stores and host services retain
        # their existing ownership. This is the same Agent and loop implementation.
        agent.tools = (original_tools if original_tools is not None else parent.tools).copy()
        agent._hooks = list(parent.hooks or ())
        agent._replaced_hooks = []
        agent.permission_engine = copy(parent.permission_engine)
        agent.permission_engine.rules = deepcopy(parent.permission_engine.rules)
        agent.permission_engine.rule_set = deepcopy(parent.permission_engine.rule_set)
        agent.tool_pipeline = copy(parent.tool_pipeline)
        agent.tool_pipeline._bus = _copy_bus(parent.tool_pipeline._bus)
        agent._turn_stopping_dispatcher = TurnStoppingDispatcher(
            parent._turn_stopping_dispatcher.snapshot()
        )
        context.register("agent", agent)
        context.register("tool_pipeline", agent.tool_pipeline)
        session.agent = agent
        for plugin, config in profile.plugins.values():
            await agent.use(plugin, json.loads(canonical_json(config)))
        eligible = set(profile.tool_allow) if profile.tool_allow is not None else None
        if original_tools is not None:
            inherited = {tool.name for tool in original_tools.list()}
            eligible = inherited if eligible is None else inherited & eligible
        restricted = agent.tools.subset(include=eligible, exclude=set(profile.tool_deny or ()))
        denied = {tool.name for tool in agent.tools.list()} - {t.name for t in restricted.list()}
        if denied:
            layers: list[PermissionLayer] = list(
                getattr(agent.permission_engine.rule_set, "layers", ())
            )
            layers.append(
                PermissionLayer("profile", [ToolRule(name, "deny") for name in sorted(denied)])
            )
            agent.permission_engine.rule_set = PermissionRuleSet(
                layers,
                project_root=getattr(
                    agent.permission_engine.rule_set,
                    "project_root",
                    agent.permission_engine.project_root,
                ),
            )
        session.tools_override = agent.tools = restricted
        if profile.system_prompt is not None:
            cfg = agent._system_prompt_config
            base = cfg.append if cfg is not None and cfg.append is not None else agent.system_prompt
            prompt = "\n\n".join(part for part in (base, profile.system_prompt) if part)
            agent.system_prompt = prompt
            if cfg is not None:
                agent._system_prompt_config = replace(cfg, append=prompt)
            if original_system is not None:
                session.system_blocks_override = [
                    *original_system,
                    SystemBlock(text=profile.system_prompt, cacheable=True),
                ]
        agent._cached_system_blocks = agent._cached_system_blocks_signature = None
        if profile.max_turns is not None:
            agent.max_turns = profile.max_turns
        resolved = profile.resolve()
        if profile.output_schema is not None:
            agent.output_schema = OutputSchema(**resolved["output_schema"])
        if profile.budget is not None:
            agent.budget = RunBudget(**resolved["budget"])
        yield
    finally:
        session.agent = parent
        session.tools_override, session.system_blocks_override = original_tools, original_system
        # Retained children and detached tools still use this run's services.
        # The parent Context owns their scope until Agent.close quiesces them.
        pending = [
            *session.background_tasks,
            *(getattr(h, "task", None) for h in session.workers.values()),
        ]
        if not any(child.agent is agent for child in parent._sessions.values()) and not any(
            task is not None and not task.done() for task in pending
        ):
            await context.dispose()
