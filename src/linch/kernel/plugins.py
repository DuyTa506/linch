from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any, Protocol

from linch.errors import ConfigError

from .. import lifecycle
from .context import Context
from .effects import CleanupError, Disposable, EffectScope


class Plugin(Protocol):
    """Describe a duck-typed plugin that can be mounted on an agent.

    Attributes:
        name: Stable human-readable plugin identifier used in diagnostics.
        version: Plugin version reported by the plugin implementation.
        provides: Service keys the plugin may register during setup.
        requires: Service keys that must resolve before setup can activate.
    """

    name: str
    version: str
    provides: Iterable[str]
    requires: Iterable[str]

    def setup(self, ctx: Context, config: Any = None) -> Disposable:
        """Configure the plugin in its fresh activation context.

        Args:
            ctx: Child context owned by this plugin activation.
            config: Optional caller-supplied plugin configuration.

        Returns:
            A disposer for cleanup not represented by context effects.
        """

        ...


_MISSING = object()


class _PluginMount:
    def __init__(self, host: Context, plugin: Plugin, config: Any) -> None:
        self.host = host
        self.plugin = plugin
        self.config = config
        self.provides = tuple(plugin.provides)
        self.requires = tuple(plugin.requires)
        self._active: tuple[Context, tuple[Any, ...]] | None = None
        self._pending_cleanup: Context | None = None
        self._busy = False
        self._pending = False
        self._closed = False

    async def start(self) -> Disposable:
        self._listener = self.host.on(lifecycle.CONTEXT_SERVICE, lambda c, k: self._reconcile(c, k))
        handle = self.host.effect(lambda: Disposable(self._unmount))
        try:
            await self._reconcile()
        except BaseException:
            await handle.dispose()
            raise
        return handle

    def _change_relevant(self, changed: Context, key: str) -> bool:
        if key not in self.requires:
            return False
        if self._pending_cleanup is not None and changed._within(self._pending_cleanup):
            return False
        current: Context | None = self.host
        while current is not None and current is not changed:
            if key in current._services:
                return False
            exported = current._plugin_exports.get(key)
            if exported is not None:
                owner, source = exported
                if changed._within(source):
                    return True
                if not owner._scope.closed and not source._scope.closed and key in source._services:
                    return False
            current = current._parent
        return current is changed

    async def _reconcile(
        self, changed: Context | None = None, changed_key: str | None = None
    ) -> None:
        if changed_key is not None and not self._change_relevant(changed or self.host, changed_key):
            return
        if self._busy:
            if self._pending_cleanup is not None:
                await self._finish_deactivation(self._pending_cleanup)
            self._pending = self._pending_cleanup is None
            return
        self._busy = True
        try:
            while True:
                self._pending = False
                if self._pending_cleanup is not None:
                    await self._finish_deactivation(self._pending_cleanup)
                requirements = tuple(self.host.get(key, _MISSING) for key in self.requires)
                ready = all(value is not _MISSING for value in requirements)
                requirements_changed = self._active is not None and not all(
                    old is new for old, new in zip(self._active[1], requirements, strict=True)
                )
                if ready and self._active is None and not self._closed:
                    await self._activate(requirements)
                elif (not ready or requirements_changed) and self._active is not None:
                    activation = self._active
                    self._active = None
                    self._pending_cleanup = activation[0]
                    if ready and not self._closed:
                        self._pending = True
                    await self._finish_deactivation(activation[0])
                if self._closed or not self._pending:
                    return
        finally:
            self._busy = False

    async def _activate(self, requirements: tuple[Any, ...]) -> None:
        pending, seen = list(self.requires), set()
        while pending:
            key = pending.pop()
            if key in self.provides:
                raise ConfigError(f"plugin dependency cycle at {key!r}")
            if key in seen:
                continue
            seen.add(key)
            if exported := self.host._plugin_exports.get(key):
                pending.extend(getattr(exported[0], "_plugin_requires", ()))
        scope = self.host.scope(f"plugin:{self.plugin.name}")
        try:
            result = scope.setup_plugin(self.plugin.setup, self.config)
            if not isinstance(result, Disposable):
                raise ConfigError(f"plugin {self.plugin.name!r} setup() must return a Disposable")
            scope.effect(lambda: result)
            services = scope._plugin_services()
            undeclared = next((key for key, _source in services if key not in self.provides), None)
            if undeclared is not None:
                raise ConfigError(f"{self.plugin.name!r}: service {undeclared!r} not in provides")
            keys = tuple(key for key, _source in services)
            if len(set(keys)) != len(keys):
                raise ConfigError(f"plugin {self.plugin.name!r} registered duplicate service keys")
            scope.__dict__["_plugin_requires"] = self.requires
            notifications = EffectScope()

            async def withdraw() -> None:
                if not notifications.closed:
                    for key in reversed(keys):
                        notification = self.host._withdraw_plugin(scope, key)
                        if notification is not None:
                            notifications.add(notification)
                await notifications.dispose()

            scope._before_dispose = Disposable(withdraw)
            for key, source in services:
                self.host._publish_plugin(scope, key, source)
            self._active = (scope, requirements)
        except BaseException as error:
            self._pending_cleanup = scope
            if isinstance(error, asyncio.CancelledError):
                raise
            try:
                await self._finish_deactivation(scope)
            except asyncio.CancelledError:
                raise
            except BaseException as cleanup_error:
                raise CleanupError((error, cleanup_error)) from error
            raise

    async def _finish_deactivation(self, scope: Context) -> None:
        await scope.dispose()
        if self._pending_cleanup is scope:
            self._pending_cleanup = None

    async def _unmount(self) -> None:
        self._closed = True
        if self._pending_cleanup is not None:
            await self._finish_deactivation(self._pending_cleanup)
        if self._active is not None:
            scope = self._active[0]
            self._active = None
            self._pending_cleanup = scope
            await self._finish_deactivation(scope)
        await self._listener.dispose()
