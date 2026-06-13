"""Memory lifecycle primitives: extraction context + a consolidation gate.

These are the *mechanism* half of ROADMAP 3.1. The extraction prompt and what
counts as a memory are embedder policy (a caller-supplied extractor callable);
this module only ships the neutral wiring the lifecycle hook consumes.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..types import Message


@dataclass(slots=True)
class MemoryExtractionContext:
    """Passed to a caller-supplied extractor on a terminal turn.

    ``history`` is the (pre-trim) ``full_history`` tail — the complete record,
    not the LLM-facing ``provider_view`` — so the extractor sees exactly what
    happened. ``store`` is the same store the hook will upsert into, handy for an
    extractor that wants to read existing entries before proposing new ones.
    """

    session: Any
    run_id: str
    turn_index: int | None
    history: list[Message] = field(default_factory=list)
    store: Any = None


# An extractor is any callable taking the context and returning candidate
# MemoryItems (sync or async). Kept as a plain callable alias — no base class.
MemoryExtractor = Callable[[MemoryExtractionContext], "Awaitable[list[Any]] | list[Any]"]


class ConsolidationGate:
    """Gate a consolidation pass on time + change-count + an in-process lock.

    Mirrors the reference lifecycle: consolidation is expensive, so it runs only
    once enough memories have changed *and* enough wall-clock has elapsed since
    the last pass. The ``asyncio.Lock`` makes it single-flight within a process;
    a multi-process lock (a store lock row) is an embedder concern and is left to
    durable store adapters.
    """

    __slots__ = ("_min_interval", "_min_changes", "_clock", "_changes", "_last_run", "_lock")

    def __init__(
        self,
        *,
        min_interval_s: float = 0.0,
        min_changes: int = 1,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._min_interval = max(0.0, float(min_interval_s))
        self._min_changes = max(1, int(min_changes))
        self._clock = clock
        self._changes = 0
        self._last_run: float | None = None
        self._lock = asyncio.Lock()

    def record(self, n: int = 1) -> None:
        """Note that *n* memories changed since the last consolidation."""
        self._changes += max(0, int(n))

    async def run(self, consolidator: Callable[[], Any]) -> bool:
        """Run *consolidator* (a zero-arg thunk) iff the gates pass.

        Returns ``True`` when it ran (and counters reset), ``False`` otherwise.
        The thunk may be sync or async.
        """
        async with self._lock:
            now = self._clock()
            if self._changes < self._min_changes:
                return False
            if self._last_run is not None and (now - self._last_run) < self._min_interval:
                return False
            # Snapshot the count we are consolidating. ``record()`` does not take
            # the lock, so increments can land while we await below; subtract only
            # the snapshot afterwards so those new changes are not lost.
            applied = self._changes
            outcome = consolidator()
            if inspect.isawaitable(outcome):
                await outcome
            self._last_run = now
            self._changes = max(0, self._changes - applied)
            return True
