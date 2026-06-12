from __future__ import annotations

import inspect
import logging
from typing import Any

_log = logging.getLogger("linch.observability")


def normalize_observers(value: Any) -> list[Any]:
    """Normalize an observer spec to a list.

    - ``None``  → ``[]``
    - A single observer → ``[observer]``
    - A list or tuple  → ``list(value)``
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


class ObserverDispatcher:
    """Exception-isolated, sync-or-async observer hub.

    Holds a list of observers and fans out a named hook call to each one,
    awaiting coroutine results and swallowing any exceptions so that a
    faulty observer never crashes the agent run.

    Usage::

        hub = ObserverDispatcher([observer])
        await hub.dispatch("on_run_start", RunInfo(...))
    """

    def __init__(self, observers: list[Any] | None = None) -> None:
        self._observers: list[Any] = list(observers or [])

    @property
    def active(self) -> bool:
        """True when at least one observer is registered."""
        return bool(self._observers)

    async def dispatch(self, hook: str, *args: Any) -> None:
        """Call *hook* on every observer, awaiting async results.

        Exceptions raised by any observer are caught and logged; they do
        **not** propagate to the caller.  Missing hooks on an observer are
        silently skipped.
        """
        if not self._observers:
            return
        for obs in self._observers:
            fn = getattr(obs, hook, None)
            if fn is None:
                continue
            try:
                result = fn(*args)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                _log.exception(
                    "observer %r raised in hook %s — continuing",
                    type(obs).__name__,
                    hook,
                )
