"""Per-agent service container with shared events and reversible child scopes."""

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
        self._teardown = Disposable(self._dispose)
        self._before_dispose: Disposable | None = None
        self._services: dict[str, Any] = {}
        self._parent = _parent
        self._children: list[Context] = []
        self._plugin_exports: dict[str, tuple[Context, Context]] = {}
        self._inbox: list[tuple[object, Callable[[Any, str], Any]]] | None = None

    def register(self, key: str, service: Any) -> Disposable:
        """Install a service and return its disposer; reject duplicate claims."""
        self._ensure_active()
        if key in self._services:
            raise ConfigError(f"service {key!r} already registered in this scope")
        if key in self._plugin_exports:
            raise ConfigError(f"service {key!r} is already provided by a plugin")

        def _install() -> Callable[[], Any]:
            notification: Disposable | None = None
            self._services[key] = service

            def _remove() -> Any:
                nonlocal notification
                if notification is None:
                    if key not in self._services or self._services[key] is not service:
                        return
                    del self._services[key]
                    if not self.events.has_listeners("context/service"):
                        return
                    notification = self.events._notification("context/service", self, key)
                return notification.dispose()

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
        self._ensure_active()
        disposer = self.events.on(name, listener, prepend=prepend)
        self._scope.add(disposer)
        return disposer

    def effect(self, fn: Callable[[], Any]) -> Disposable:
        """Register a reversible effect owned by this scope."""
        self._ensure_active()
        return self._scope.effect(fn)

    def inbox(self, producer: Callable[[Any, str], Any]) -> Disposable:
        self._ensure_active()
        target = getattr(self.get("agent"), "context", self)
        bucket = target._inbox = target._inbox or []
        entry = (object(), producer)
        bucket.append(entry)
        return self._scope.add(Disposable(lambda: bucket.remove(entry)))

    def scope(self, label: str = "scope") -> Context:
        """Return a child sharing the bus with isolated services and teardown."""
        self._ensure_active()
        child = Context(label=label, _bus=self.events, _parent=self)
        self._children.append(child)
        self._scope.add(child._teardown)
        return child

    def _ensure_active(self) -> None:
        self._scope.ensure_active()
        if (active_scope := _PLUGIN_SCOPE.get()) is not None and not self._within(active_scope):
            raise ConfigError("plugin setup may only register effects in its activation scope")

    def _within(self, ancestor: Context) -> bool:
        current: Context | None = self
        while current is not None:
            if current is ancestor:
                return True
            current = current._parent
        return False

    def _plugin_services(self) -> list[tuple[str, Context]]:
        result: list[tuple[str, Context]] = [(key, self) for key in self._services]
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

    async def _dispose(self) -> None:
        self._scope._closed = True
        if self._before_dispose is not None:
            await self._before_dispose.dispose()
        await self._scope.dispose()
        if self._parent is not None:
            self._parent._children.remove(self)

    async def dispose(self) -> None:
        await self._teardown.dispose()
