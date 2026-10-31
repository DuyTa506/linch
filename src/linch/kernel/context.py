"""``Context`` — the per-agent service container and extension surface.

Ported from Cordis' ``Context`` (deepseek-harness/vendor/cordis/src/context.ts).
A :class:`Context` owns a service registry, a shared :class:`~linch.kernel.events.EventBus`,
and an :class:`~linch.kernel.effects.EffectScope`. Services are read either as
``ctx.<name>`` (a declared dependency — raises if absent) or ``ctx.get(name)``
(optional). ``register``/``on``/``effect`` are reversible: their teardown is owned
by this context's scope, so disposing the context unwinds exactly its own
contributions. ``scope()`` returns a child that shares the bus but overlays its
own services and scope — the per-agent isolation that lets N agents share one
process without global state.
"""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from typing import Any

from linch.errors import ConfigError

from .effects import Disposable, EffectScope
from .events import EventBus, Listener

_MISSING = object()
_PLUGIN_SCOPE: ContextVar[Any] = ContextVar("linch_plugin_scope", default=None)


class Context:
    """A service container with a typed event bus and reversible registrations."""

    def __init__(
        self,
        *,
        label: str = "root",
        _bus: EventBus | None = None,
        _parent: Context | None = None,
    ) -> None:
        self.label = label
        self.events: EventBus = _bus if _bus is not None else EventBus()
        self._scope = EffectScope()
        self._services: dict[str, Any] = {}
        self._parent = _parent
        self._children: list[Context] = []
        self._plugin_exports: dict[str, tuple[Context, Context]] = {}

    def register(self, key: str, service: Any) -> Disposable:
        """Install *service* under *key* in this scope; returns a disposer.

        Raises :class:`ConfigError` if *key* is already registered in this scope
        (a child may still shadow a parent's service under the same key).
        """
        self._scope.ensure_active()
        active_scope = _PLUGIN_SCOPE.get()
        if active_scope is not None and not self._within(active_scope):
            raise ConfigError("plugin setup may only register services in its activation scope")
        if key in self._services:
            raise ConfigError(f"service {key!r} already registered in this scope")
        if key in self._plugin_exports:
            raise ConfigError(f"service {key!r} is already provided by a plugin")

        def _install() -> Callable[[], Any]:
            removed = False
            notified = False
            notification: Disposable | None = None
            self._services[key] = service

            async def _remove() -> None:
                nonlocal notified, notification, removed
                if not removed:
                    if key not in self._services or self._services[key] is not service:
                        return
                    del self._services[key]
                    removed = True
                if notified:
                    return
                if notification is None:
                    if not self.events.has_listeners("context/service"):
                        notified = True
                        return
                    notification = self.events._notification("context/service", self, key)
                await notification.dispose()
                notified = True

            return _remove

        disposer = self._scope.effect(_install)
        if self.events.has_listeners("context/service") and _PLUGIN_SCOPE.get() is None:
            self.events.emit("context/service", self, key)
        return disposer

    def get(self, key: str, default: Any = None) -> Any:
        """Read a service, walking up to parent scopes; *default* if unresolved."""
        if not self._scope.closed and key in self._services:
            return self._services[key]
        exported = self._plugin_exports.get(key) if not self._scope.closed else None
        if exported is not None:
            owner, source = exported
            if not owner._scope.closed and not source._scope.closed:
                service = source._services.get(key, _MISSING)
                if service is not _MISSING:
                    return service
        if self._parent is not None:
            return self._parent.get(key, default)
        return default

    def __getattr__(self, name: str) -> Any:
        # Only fires for attributes not found normally. Underscore/dunder names
        # are never services and must raise so normal lookup (and no infinite
        # recursion through _services/_parent) is preserved.
        if name.startswith("_"):
            raise AttributeError(name)
        service = self.get(name, _MISSING)
        if service is _MISSING:
            raise AttributeError(f"no service {name!r} registered on context {self.label!r}")
        return service

    def on(self, name: str, listener: Listener, *, prepend: bool = False) -> Disposable:
        """Register an event listener owned by this scope (disposed with it)."""
        # Validate first: registering on the bus before a closed-scope failure
        # would leak an unowned listener.
        self._scope.ensure_active()
        disposer = self.events.on(name, listener, prepend=prepend)
        self._scope.add(disposer)
        return disposer

    def effect(self, fn: Callable[[], Any]) -> Disposable:
        """Register a reversible effect owned by this scope."""
        return self._scope.effect(fn)

    def scope(self, label: str = "scope") -> Context:
        """Return a child context sharing the bus but isolating services/teardown.

        Child registrations are invisible to the parent and are unwound when the
        child (or the parent) is disposed.
        """
        self._scope.ensure_active()
        active_scope = _PLUGIN_SCOPE.get()
        if active_scope is not None and not self._within(active_scope):
            raise ConfigError("plugin setup may only create scopes in its activation scope")
        child = Context(label=label, _bus=self.events, _parent=self)
        self._children.append(child)
        self._scope.add(Disposable(child.dispose))
        return child

    def _within(self, ancestor: Context) -> bool:
        current: Context | None = self
        while current is not None:
            if current is ancestor:
                return True
            current = current._parent
        return False

    def _plugin_services(self) -> list[tuple[str, Context]]:
        services = list(self._services.items())
        result: list[tuple[str, Context]] = [(key, self) for key, _service in services]
        for child in self._children:
            result.extend(child._plugin_services())
        return result

    def _publish_plugin(self, owner: Context, key: str, source: Context) -> None:
        if key in self._services:
            raise ConfigError(f"service {key!r} already exists in the mounting context")
        existing = self._plugin_exports.get(key)
        if existing is not None and existing[0] is not owner:
            raise ConfigError(f"service {key!r} is already provided by another plugin")
        self._plugin_exports[key] = (owner, source)
        if self.events.has_listeners("context/service"):
            self.events.emit("context/service", self, key)

    def _withdraw_plugin(self, owner: Context, key: str) -> Disposable | None:
        existing = self._plugin_exports.get(key)
        if existing is None or existing[0] is not owner:
            return None
        del self._plugin_exports[key]
        if self.events.has_listeners("context/service"):
            return self.events._notification("context/service", self, key)
        return None

    async def dispose(self) -> None:
        """Unwind every registration, listener, and child scope owned here."""
        await self._scope.dispose()
        if self._parent is not None:
            try:
                self._parent._children.remove(self)
            except ValueError:
                pass
