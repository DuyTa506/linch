from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from .errors import AbortError


@dataclass(slots=True)
class AbortContext:
    _event: asyncio.Event = field(default_factory=asyncio.Event)
    _watch_task: asyncio.Task[None] | None = field(default=None)

    def abort(self) -> None:
        self._event.set()

    def close(self) -> None:
        """Release the merged-signal watcher (if any) without aborting.

        Self-contained cleanup for signals created by ``any_signal``: cancels
        the watcher task, which then cancels its pending child waiters in
        ``finally``. Does not set ``aborted``. Safe to call on a plain
        ``AbortContext`` (no-op when there is no watcher).
        """
        if self._watch_task is not None and not self._watch_task.done():
            self._watch_task.cancel()

    @property
    def aborted(self) -> bool:
        return self._event.is_set()

    def throw_if_aborted(self) -> None:
        if self._event.is_set():
            raise AbortError("operation aborted")

    async def wait(self) -> None:
        """Block until this signal is aborted. Public API for awaiting an abort."""
        await self._event.wait()


def throw_if_aborted(signal: AbortContext | None) -> None:
    if signal is not None:
        signal.throw_if_aborted()


def any_signal(*signals: AbortContext | None) -> AbortContext:
    merged = AbortContext()
    for sig in signals:
        if sig is not None and sig.aborted:
            merged.abort()
            return merged

    inputs = [sig for sig in signals if sig is not None]
    if not inputs:
        return merged

    async def _watch() -> None:
        # Wait on the input events AND the merged signal's own event, so the
        # watcher terminates when any input aborts OR the merged signal is
        # aborted/closed — never parking forever after the run completes.
        watchers = [asyncio.ensure_future(sig._event.wait()) for sig in inputs]
        watchers.append(asyncio.ensure_future(merged._event.wait()))
        try:
            done, _ = await asyncio.wait(watchers, return_when=asyncio.FIRST_COMPLETED)
            # Any input firing aborts the merged signal; the merged-event
            # waiter completing means it was already aborted/closed.
            if any(w in done for w in watchers[:-1]):
                merged.abort()
        finally:
            # Cancel still-pending child waiters so no Event.wait() futures
            # dangle on the input/merged events.
            for w in watchers:
                if not w.done():
                    w.cancel()

    merged._watch_task = asyncio.create_task(_watch())
    return merged
