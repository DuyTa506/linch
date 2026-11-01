"""A typed, async-first event bus with three dispatch modes.

Ported from Cordis' event service (deepseek-harness/vendor/cordis/src/events.ts):

- :meth:`EventBus.emit` — non-vetoing broadcast; each listener is isolated in its
  own ``try/except`` so one failure cannot starve the rest.
- :meth:`EventBus.serial` — await listeners in order, stop at the first that
  returns a non-``None``/non-``False`` value ("bail").
- :meth:`EventBus.waterfall` — a middleware chain: each listener receives a
  ``next`` callable as its last argument and MUST ``await next()`` to delegate to
  the rest of the chain (and finally the built-in terminal). A listener that
  returns without calling ``next`` short-circuits the chain (a veto).

``on`` returns a :class:`~linch.kernel.effects.Disposable` that unregisters the
listener, so registrations compose with :class:`~linch.kernel.effects.EffectScope`.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import Any, Literal

from .effects import Disposable, _dispose_reverse

logger = logging.getLogger("linch.kernel.events")

DispatchMode = Literal["emit", "serial", "waterfall", "hook"]


@dataclass(frozen=True, slots=True)
class LifecyclePoint:
    """Name one lifecycle point and its payload and dispatch contracts."""

    name: str
    _payload_ref: str | tuple[str, ...]
    mode: DispatchMode

    @property
    def payload_type(self) -> type[Any] | tuple[type[Any], ...]:
        """Resolve and return the payload type(s) for this point."""
        refs = self._payload_ref if isinstance(self._payload_ref, tuple) else (self._payload_ref,)
        resolved = tuple(_resolve_type(ref) for ref in refs)
        return resolved[0] if len(resolved) == 1 else resolved


def _resolve_type(ref: str) -> type[Any]:
    import importlib

    module_name, _, qualname = ref.rpartition(".")
    value: Any = importlib.import_module(module_name)
    for part in qualname.split("."):
        value = getattr(value, part)
    return value


CONTEXT_SERVICE = LifecyclePoint(
    "context/service", ("linch.kernel.context.Context", "builtins.str"), "emit"
)

Listener = Callable[..., Any]
Next = Callable[[], Awaitable[Any]]
EventName = str | LifecyclePoint


async def _resolve(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


class EventBus:
    """A name-keyed registry of listeners dispatched via emit/serial/waterfall."""

    __slots__ = ("_listener_tokens", "_listeners")

    def __init__(self) -> None:
        # Keep listener values directly in this mapping for compatibility with
        # the pipeline's ordered listener introspection. A parallel token list
        # gives each otherwise-duplicate registration its own identity.
        self._listeners: dict[str, list[Listener]] = {}
        self._listener_tokens: dict[str, list[object]] = {}

    def on(self, name: EventName, listener: Listener, *, prepend: bool = False) -> Disposable:
        """Register *listener* for *name*; returns a disposer that removes it."""
        name = _event_name(name)
        bucket = self._listeners.setdefault(name, [])
        tokens = self._listener_tokens.setdefault(name, [])
        token = object()
        if prepend:
            bucket.insert(0, listener)
            tokens.insert(0, token)
        else:
            bucket.append(listener)
            tokens.append(token)

        def _remove() -> None:
            for index, item in enumerate(tokens):
                if item is token:
                    tokens.pop(index)
                    bucket.pop(index)
                    break

        return Disposable(_remove)

    def has_listeners(self, name: EventName) -> bool:
        """Whether any listener is currently registered for *name*."""
        name = _event_name(name)
        return bool(self._listeners.get(name))

    def fork(self, *, exclude: tuple[EventName, ...] = ()) -> EventBus:
        """Snapshot listeners into an independently mutable bus.

        Args:
            exclude: Event names whose registrations must stay on this bus.

        Returns:
            A bus preserving listener order with independent registration ownership.
        """
        skipped = {_event_name(name) for name in exclude}
        bus = EventBus()
        for name, listeners in self._listeners.items():
            if name not in skipped:
                for listener in listeners:
                    bus.on(name, listener)
        return bus

    def emit(self, name: EventName, *args: Any) -> None:
        """Broadcast to every listener, isolating failures. Non-vetoing.

        Sync listeners run inline; their ``CancelledError`` propagates as
        cancellation control flow rather than being logged as an ordinary
        failure. A listener returning a coroutine has it scheduled as an
        independent task: its cancellation is task-local and not logged. If no
        loop is running the coroutine is closed and a warning is logged rather
        than leaking an un-awaited coroutine.
        """
        name = _event_name(name, mode="emit")
        for listener in list(self._listeners.get(name, ())):
            try:
                result = listener(*args)
            except Exception:
                logger.exception("emit listener for %r failed", name)
                continue
            if inspect.isawaitable(result):
                _schedule(result, name)

    async def serial(self, name: EventName, *args: Any) -> Any:
        """Await listeners in order; return the first non-``None``/``False`` value."""
        name = _event_name(name, mode="serial")
        for listener in list(self._listeners.get(name, ())):
            result = await _resolve(listener(*args))
            if result is not None and result is not False:
                return result
        return None

    def _notification(self, name: EventName, *args: Any) -> Disposable:
        """Snapshot a broadcast as a retryable, ordered disposable."""
        name = _event_name(name, mode="emit")
        listeners = [Disposable(partial(listener, *args)) for listener in self._listeners[name]]
        return Disposable(lambda: _dispose_reverse(list(reversed(listeners))))

    async def waterfall(self, name: EventName, *args: Any, next: Next) -> Any:
        """Run listeners as a middleware chain ending in the built-in *next*.

        Each listener is called as ``listener(*args, next)`` and delegates by
        ``await next()`` exactly once. The outermost listener wraps the next,
        and so on, with *next* (the built-in behavior) at the center.
        """
        name = _event_name(name, mode="waterfall")
        listeners = list(self._listeners.get(name, ()))

        async def dispatch(index: int) -> Any:
            if index >= len(listeners):
                return await _resolve(next())

            called = False

            async def _next() -> Any:
                nonlocal called
                if called:
                    raise RuntimeError("waterfall next() may only be called once")
                # Consume before dispatching so concurrent calls cannot both
                # enter the downstream chain.
                called = True
                return await dispatch(index + 1)

            return await _resolve(listeners[index](*args, _next))

        return await dispatch(0)


def _event_name(name: EventName, *, mode: str | None = None) -> str:
    if isinstance(name, LifecyclePoint):
        if mode is not None and name.mode != mode:
            raise ValueError(
                f"lifecycle point {name.name!r} uses {name.mode} dispatch; "
                f"cannot dispatch with {mode}"
            )
        return name.name
    if not isinstance(name, str):
        raise TypeError("event name must be a string or LifecyclePoint")
    return name


def _schedule(coro: Awaitable[Any], name: str) -> None:
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop: we cannot await it here. Close it to avoid a
        # "coroutine was never awaited" warning and surface the misuse.
        close = getattr(coro, "close", None)
        if callable(close):
            close()
        logger.warning("emit listener for %r returned a coroutine with no running loop", name)
        return

    task = loop.create_task(_resolve(coro))

    def _log(t: asyncio.Task[Any]) -> None:
        if not t.cancelled() and t.exception() is not None:
            logger.error("async emit listener for %r failed: %r", name, t.exception())

    task.add_done_callback(_log)
