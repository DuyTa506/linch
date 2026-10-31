"""Duck-typed plugins and scoped activation for the agent kernel."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

from linch.errors import ConfigError

from .context import _PLUGIN_SCOPE, Context
from .effects import CleanupError, Disposable


class Plugin(Protocol):
    """A reversible, service-oriented kernel extension."""

    name: str
    version: str
    provides: Iterable[str]
    requires: Iterable[str]

    def setup(self, ctx: Context, config: Any = None) -> Disposable: ...


_MISSING = object()


@dataclass(slots=True)
class _Activation:
    scope: Context
    keys: tuple[str, ...]
    requirements: tuple[tuple[str, Any], ...] = ()
    notifications: dict[str, Disposable] = field(default_factory=dict)


class _PluginMount:
    def __init__(self, host: Context, plugin: Plugin, config: Any) -> None:
        self.host = host
        self.plugin = plugin
        self.config = config
        self.provides = tuple(getattr(plugin, "provides", ()))
        self.requires = tuple(getattr(plugin, "requires", ()))
        self._active: _Activation | None = None
        self._pending_cleanup: _Activation | None = None
        self._busy = False
        self._pending = False
        self._closed = False
        self._listener: Disposable | None = None

    async def start(self) -> Disposable:
        self._listener = self.host.on("context/service", self._on_service_change)
        handle = self.host.effect(lambda: Disposable(self._unmount))
        try:
            await self._reconcile()
        except BaseException:
            await handle.dispose()
            raise
        return handle

    async def _on_service_change(self, _changed: Context, _key: str) -> None:
        if not self._closed:
            await self._reconcile()

    async def _reconcile(self) -> None:
        if self._closed:
            return
        if self._busy:
            self._pending = True
            return
        self._busy = True
        try:
            while True:
                self._pending = False
                if self._pending_cleanup is not None:
                    await self._finish_deactivation(self._pending_cleanup)
                requirements = tuple((key, self.host.get(key, _MISSING)) for key in self.requires)
                ready = all(value is not _MISSING for _key, value in requirements)
                changed = self._active is not None and not all(
                    old_value is value
                    for (_key, old_value), (_new_key, value) in zip(
                        self._active.requirements, requirements, strict=True
                    )
                )
                if ready and self._active is None and not self._closed:
                    await self._activate()
                elif (not ready or changed) and self._active is not None:
                    activation = self._active
                    self._active = None
                    self._pending_cleanup = activation
                    if ready and not self._closed:
                        self._pending = True
                    await self._finish_deactivation(activation)
                if self._closed:
                    return
                if not self._pending:
                    break
        finally:
            self._busy = False

    async def _activate(self) -> None:
        scope = self.host.scope(f"plugin:{self.plugin.name}")
        requirements = tuple((key, self.host.get(key, _MISSING)) for key in self.requires)
        activation: _Activation | None = None
        try:
            token = _PLUGIN_SCOPE.set(scope)
            try:
                result = self.plugin.setup(scope, self.config)
            finally:
                _PLUGIN_SCOPE.reset(token)
            if not isinstance(result, Disposable):
                raise ConfigError(f"plugin {self.plugin.name!r} setup() must return a Disposable")
            scope.effect(lambda: result)
            services = scope._plugin_services()
            declared = set(self.provides)
            undeclared = [key for key, _source in services if key not in declared]
            if undeclared:
                raise ConfigError(
                    f"plugin {self.plugin.name!r} registered {undeclared[0]!r}, "
                    "which is not declared in provides"
                )
            keys = tuple(key for key, _source in services)
            if len(set(keys)) != len(keys):
                raise ConfigError(f"plugin {self.plugin.name!r} registered duplicate service keys")
            activation = _Activation(scope=scope, keys=keys, requirements=requirements)
            for key, source in services:
                self.host._publish_plugin(scope, key, source)
            self._active = activation
        except BaseException as error:
            if activation is None:
                activation = _Activation(
                    scope=scope,
                    keys=tuple(key for key, _source in scope._plugin_services()),
                )
            try:
                await self._finish_deactivation(activation)
            except BaseException as cleanup_error:
                raise CleanupError((error, cleanup_error)) from error
            raise

    async def _finish_deactivation(self, activation: _Activation) -> None:
        notifications: list[Disposable] = []
        for key in activation.keys:
            notification = activation.notifications.get(key)
            if notification is None:
                notification = self.host._withdraw_plugin(activation.scope, key)
                if notification is not None:
                    activation.notifications[key] = notification
            if notification is not None:
                notifications.append(notification)
        errors: list[BaseException] = []
        for notification in notifications:
            try:
                await notification.dispose()
            except BaseException as error:
                errors.append(error)
        if errors:
            raise errors[0] if len(errors) == 1 else CleanupError(errors)
        await activation.scope.dispose()
        if self._pending_cleanup is activation:
            self._pending_cleanup = None

    async def _unmount(self) -> None:
        self._closed = True
        if self._listener is not None:
            await self._listener.dispose()
        if self._pending_cleanup is not None:
            await self._finish_deactivation(self._pending_cleanup)
        if self._active is not None:
            activation = self._active
            self._active = None
            self._pending_cleanup = activation
            await self._finish_deactivation(activation)


async def mount_plugin(ctx: Context, plugin: Plugin, config: Any = None) -> Disposable:
    """Mount *plugin* into *ctx*, returning an idempotent unmount handle."""
    return await _PluginMount(ctx, plugin, config).start()
