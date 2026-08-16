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
from typing import Any

from linch.errors import ConfigError

from .effects import Disposable, EffectScope
from .events import EventBus, Listener

_MISSING = object()


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

    def register(self, key: str, service: Any) -> Disposable:
        """Install *service* under *key* in this scope; returns a disposer.

        Raises :class:`ConfigError` if *key* is already registered in this scope
        (a child may still shadow a parent's service under the same key).
        """
        self._scope.ensure_active()
        if key in self._services:
            raise ConfigError(f"service {key!r} already registered in this scope")

        def _install() -> Callable[[], None]:
            self._services[key] = service

            def _remove() -> None:
                if key in self._services and self._services[key] is service:
                    del self._services[key]

            return _remove

        return self._scope.effect(_install)

    def get(self, key: str, default: Any = None) -> Any:
        """Read a service, walking up to parent scopes; *default* if unresolved."""
        if key in self._services:
            return self._services[key]
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
        child = Context(label=label, _bus=self.events, _parent=self)
        self._scope.add(Disposable(child._scope.dispose))
        return child

    async def dispose(self) -> None:
        """Unwind every registration, listener, and child scope owned here."""
        await self._scope.dispose()
