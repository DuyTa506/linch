"""``SchedulerLoop`` — an async time-trigger that fires due schedules.

The loop ticks once per second (an ``asyncio`` task, never a thread). On each
tick it fires every due schedule into the bound session's
``pending_notifications`` — the same drain background workers use, so a fired
schedule surfaces as a ``UserEvent`` on the next turn — recomputes the next run,
and persists it back to the store. ``tick()`` is the pure, testable core; the
1-second cadence is just ``tick`` + ``asyncio.sleep`` in a loop.

What a schedule *means* (its payload) is embedder policy; the loop only delivers.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any
from xml.sax.saxutils import escape

from ...events import ScheduleEvent
from ...types import Message, TextBlock
from .schedule import Schedule
from .store import ClaimingScheduleStore, ScheduleStore


def render_schedule_message(schedule: Schedule) -> Message:
    """Wrap a fired schedule as a ``<scheduled-task>`` user message."""
    parts = [
        "<scheduled-task>",
        f"<id>{escape(schedule.id)}</id>",
        f"<payload>{escape(schedule.payload)}</payload>",
        "</scheduled-task>",
    ]
    return Message(role="user", content=[TextBlock(text="".join(parts))])


class SchedulerLoop:
    def __init__(
        self,
        store: ScheduleStore,
        session: Any,
        *,
        clock: Callable[[], float] = time.time,
        tick_s: float = 1.0,
        on_event: Callable[[ScheduleEvent], Any] | None = None,
    ) -> None:
        self._store = store
        self._session = session
        self._clock = clock
        self._tick_s = tick_s
        self._on_event = on_event
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> asyncio.Task[None]:
        """Spawn the background tick loop (idempotent while already running)."""
        if self.running:
            assert self._task is not None
            return self._task
        self._task = asyncio.ensure_future(self._run())
        return self._task

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def tick(self) -> list[Schedule]:
        """Fire every schedule whose ``next_run`` is due. Returns those fired."""
        now = self._clock()
        if isinstance(self._store, ClaimingScheduleStore):
            fired = await self._store.claim_due(now)
            for schedule in fired:
                # claim_due has already advanced+committed each next_run, so a
                # raising _fire must not abort the remaining already-claimed
                # schedules this tick — that would silently drop them. Delivery
                # (the notification append) precedes the on_event sink in _fire,
                # so an exception here means only the observability sink failed.
                try:
                    await self._fire(schedule)
                except Exception:
                    # Sibling isolation: one schedule's failure (almost always
                    # the on_event sink, since delivery precedes it) must not
                    # abort the others already claimed this tick. Log so the
                    # drop isn't silent — next_run is already committed, so a
                    # claimed schedule cannot be retried next tick.
                    logging.getLogger(__name__).warning(
                        "SchedulerLoop: _fire failed for schedule %r", schedule.id, exc_info=True
                    )
            return fired

        fired: list[Schedule] = []
        for schedule in await self._store.list():
            if not schedule.enabled or schedule.next_run is None:
                continue
            if schedule.next_run > now:
                continue
            await self._fire(schedule)
            schedule.next_run = schedule.compute_next_run(now)
            await self._store.update(schedule)
            fired.append(schedule)
        return fired

    async def _fire(self, schedule: Schedule) -> None:
        notifications = getattr(self._session, "pending_notifications", None)
        if notifications is not None:
            notifications.append(render_schedule_message(schedule))
        if self._on_event is not None:
            outcome = self._on_event(
                ScheduleEvent(schedule_id=schedule.id, status="fired", payload=schedule.payload)
            )
            if asyncio.iscoroutine(outcome):
                await outcome

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A misbehaving store/sink must not kill the loop; the next tick
                # retries. (Matches the swallow-and-continue background contract.)
                pass
            await asyncio.sleep(self._tick_s)
