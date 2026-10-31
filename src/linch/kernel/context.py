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
    """Own services, effects, and extension registrations for one agent tree.

    Child contexts share the event bus but keep service registrations isolated;
    lookups fall back to parent contexts.
    """

    def __init__(
        self,
        *,
        label: str = "root",
        _bus: EventBus | None = None,
        _parent: Context | None = None,
    ) -> None:
        """Create a context, optionally as a child of another context.

        Args:
            label: Human-readable label used in diagnostics.
            _bus: Internal event bus shared with a parent or child scope.
            _parent: Internal parent whose services are visible as fallbacks.
        """
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
        """Install a service and return the disposer for that registration.

        Args:
            key: Service key to make available in this context.
            service: Service value, including falsey values.

        Returns:
            A handle that removes exactly this registration. Removal awaits
            interested service-change listeners and can be retried after a
            listener failure.

        Raises:
            ConfigError: If this scope or a plugin export already claims the key,
                or registration would escape the active plugin setup scope.
            RuntimeError: If this context is closing or already disposed.
        """
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
        """Read a service, falling back through parent scopes if unresolved.

        Args:
            key: Service key to resolve.
            default: Value returned when no live registration resolves the key.

        Returns:
            The nearest live service value, or ``default`` when unavailable.
        """
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
        """Register an event listener owned by this context.

        Args:
            name: Event name to listen for.
            listener: Callable invoked by the event bus.
            prepend: Whether to run this listener before existing listeners.

        Returns:
            A disposer that unregisters this listener.

        Raises:
            ConfigError: If plugin setup tries to register outside its scope.
            RuntimeError: If this context is closing or already disposed.
        """
        # Validate first: registering on the bus before a closed-scope failure
        # would leak an unowned listener.
        self._ensure_active()
        disposer = self.events.on(name, listener, prepend=prepend)
        self._scope.add(disposer)
        return disposer

    def effect(self, fn: Callable[[], Any]) -> Disposable:
        """Run and own a reversible effect in this context.

        Args:
            fn: Callable that performs the effect and returns its disposer.

        Returns:
            A disposer for this effect, also retained by context teardown. The
            callback may return no cleanup, one disposer, or a group of
            disposers.

        Raises:
            ConfigError: If plugin setup tries to register outside its scope.
            RuntimeError: If this context is closing or already disposed.
            TypeError: If the callback returns an unsupported cleanup value.
        """
        self._ensure_active()
        return self._scope.effect(fn)

    def setup_plugin(
        self, setup: Callable[[Context, Any], Disposable], config: Any = None
    ) -> Disposable:
        """Run a synchronous plugin setup callback under this context's guard.

        The guard is restored in a ``finally`` block, including when the
        callback raises; callback exceptions propagate unchanged.

        Args:
            setup: Plugin callback receiving this activation context and config.
            config: Optional plugin-specific configuration.

        Returns:
            The disposer returned by the setup callback.

        Raises:
            ConfigError: If setup tries to mutate a context outside this scope.
            RuntimeError: If this context is closing or already disposed.
        """
        self._ensure_active()
        token = _PLUGIN_SCOPE.set(self)
        try:
            return setup(self, config)
        finally:
            _PLUGIN_SCOPE.reset(token)

    def inbox(self, producer: Callable[[Any, str], Any]) -> Disposable:
        """Register an inbox producer owned by this context.

        Delivery targets the resolved agent's inbox in registration order, but
        the registering context owns cleanup. The loop logs message items as
        notification events and persists already-logged event items without
        another history append. It awaits an iterator's optional ``aclose()``
        on completion, failure, or consumer closure.

        Args:
            producer: Callable receiving a session and run identifier, returning
                an async iterator of messages or already-logged events.

        Returns:
            A disposer that removes exactly this producer registration.

        Raises:
            ConfigError: If plugin setup tries to register outside its scope.
            RuntimeError: If this context is closing or already disposed.
        """
        self._ensure_active()
        target = getattr(self.get("agent"), "context", self)
        bucket = target._inbox = target._inbox or []
        entry = (object(), producer)
        bucket.append(entry)
        return self._scope.add(Disposable(lambda: bucket.remove(entry)))

    def scope(self, label: str = "scope", *, isolate_events: bool = False) -> Context:
        """Create an owned child with isolated services and optional event isolation.

        Profile plugins need isolated listener registrations so they cannot affect
        other runs on the same Agent. Parent service changes still reach the child.

        Args:
            label: Human-readable label for the child context.
            isolate_events: Whether to snapshot listeners into an independent bus.

        Returns:
            A child context whose teardown is owned by this context.

        Raises:
            ConfigError: If plugin setup tries to create a scope outside its
                activation scope.
            RuntimeError: If this context is closing or already disposed.
        """
        self._ensure_active()
        bus = self.events.fork(exclude=("context/service",)) if isolate_events else self.events
        child = Context(label=label, _bus=bus, _parent=self)
        self._children.append(child)
        self._scope.add(child._teardown)
        if isolate_events:

            async def service_changed(changed: Context, key: str) -> None:
                if child._within(changed) and bus.has_listeners("context/service"):
                    await bus._notification("context/service", changed, key).dispose()

            child.effect(lambda: self.events.on("context/service", service_changed))
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
        """Dispose this context and await all owned cleanup.

        Plugin exports are withdrawn before asynchronous cleanup begins. Failed
        cleanup remains retryable through a later call.

        Raises:
            BaseException: An owned cleanup failure after all cleanup attempts.
        """
        await self._teardown.dispose()
