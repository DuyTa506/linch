from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from .errors import AbortError


@dataclass(slots=True)
class AbortContext:
    _event: asyncio.Event = field(default_factory=asyncio.Event)

    def abort(self) -> None:
        self._event.set()

    @property
    def aborted(self) -> bool:
        return self._event.is_set()

    def throw_if_aborted(self) -> None:
        if self._event.is_set():
            raise AbortError("operation aborted")


def throw_if_aborted(signal: AbortContext | None) -> None:
    if signal is not None:
        signal.throw_if_aborted()


def any_signal(*signals: AbortContext | None) -> AbortContext:
    merged = AbortContext()
    for sig in signals:
        if sig is not None and sig.aborted:
            merged.abort()
            return merged

    async def _watch() -> None:
        watchers = [sig._event.wait() for sig in signals if sig is not None]
        if watchers:
            done, _ = await asyncio.wait(watchers, return_when=asyncio.FIRST_COMPLETED)
            if done:
                merged.abort()

    asyncio.create_task(_watch())
    return merged
