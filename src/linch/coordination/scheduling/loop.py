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
from collections.abc import Awaitable, Callable
from typing import Any, cast
from uuid import uuid4
from xml.sax.saxutils import escape

from ...errors import ConfigError
from ...events import ScheduleEvent
from ...types import Message, TextBlock
from .schedule import Schedule, ScheduleOccurrence
from .store import ClaimingScheduleStore, LeasedScheduleStore, ScheduleStore


def render_schedule_message(schedule: Schedule) -> Message:
    """Wrap a fired schedule as a ``<scheduled-task>`` user message."""
    parts = [
        "<scheduled-task>",
        f"<id>{escape(schedule.id)}</id>",
        f"<payload>{escape(schedule.payload)}</payload>",
        "</scheduled-task>",
    ]
    return Message(role="user", content=[TextBlock(text="".join(parts))])


def _render_occurrence_message(occurrence: ScheduleOccurrence) -> Message:
    parts = [
        "<scheduled-task>",
        f"<id>{escape(occurrence.schedule_id)}</id>",
        f"<payload>{escape(occurrence.payload)}</payload>",
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
        session_id = getattr(session, "id", "unknown")
        self._lease_owner = f"scheduler:{session_id}:{uuid4().hex}"

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

    async def tick(self) -> list[Schedule | ScheduleOccurrence]:
        """Fire every schedule whose ``next_run`` is due. Returns those fired."""
        now = self._clock()
        durability = getattr(getattr(self._session, "agent", None), "durability", None)
        if bool(getattr(durability, "durable_inbox", False)):
            if not isinstance(self._store, LeasedScheduleStore):
                raise ConfigError(
                    "durable inbox scheduling requires a LeasedScheduleStore with "
                    "claim_due_occurrences(), ack_occurrences(), and release_occurrences()"
                )
            lease_s = float(getattr(durability, "delivery_lease_s", 30.0))
            occurrences = await self._store.claim_due_occurrences(
                now,
                owner=self._lease_owner,
                lease_s=lease_s,
            )
            delivered: list[ScheduleOccurrence] = []
            for occurrence in occurrences:
                try:
                    await self._deliver_occurrence(occurrence)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logging.getLogger(__name__).warning(
                        "SchedulerLoop: durable delivery failed for occurrence %r",
                        occurrence.id,
                        exc_info=True,
                    )
                    continue
                delivered.append(occurrence)
            return cast("list[Schedule | ScheduleOccurrence]", delivered)

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
            return cast("list[Schedule | ScheduleOccurrence]", fired)

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
        return cast("list[Schedule | ScheduleOccurrence]", fired)

    async def _deliver_occurrence(self, occurrence: ScheduleOccurrence) -> None:
        assert isinstance(self._store, LeasedScheduleStore)
        try:
            notify = getattr(self._session, "notify", None)
            if not callable(notify):
                raise ConfigError("durable inbox scheduling requires Session.notify()")
            await cast(
                "Awaitable[str]",
                notify(
                    _render_occurrence_message(occurrence),
                    delivery_id=f"schedule:{occurrence.id}",
                    source="schedule",
                    metadata={
                        "schedule_id": occurrence.schedule_id,
                        "occurrence_id": occurrence.id,
                    },
                ),
            )
            await self._store.ack_occurrences([occurrence])
        except BaseException:
            # If notify committed before its response was lost, releasing and
            # redelivering is safe: Session.notify deduplicates the stable
            # delivery id. Token fencing makes this a no-op after a successful
            # ack or after another consumer has reclaimed the occurrence.
            release = asyncio.create_task(self._release_occurrence_safely(occurrence))
            try:
                await asyncio.shield(release)
            except asyncio.CancelledError:
                # The release task retains its own reference and continues to
                # completion even when this scheduler tick is being cancelled.
                pass
            raise

        # Observability is deliberately outside the delivery/ack transaction.
        # A sink failure must never make a completed occurrence retry.
        if self._on_event is not None:
            try:
                outcome = self._on_event(
                    ScheduleEvent(
                        schedule_id=occurrence.schedule_id,
                        status="fired",
                        payload=occurrence.payload,
                    )
                )
                if asyncio.iscoroutine(outcome):
                    await outcome
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.getLogger(__name__).warning(
                    "SchedulerLoop: event sink failed for occurrence %r",
                    occurrence.id,
                    exc_info=True,
                )

    async def _release_occurrence_safely(self, occurrence: ScheduleOccurrence) -> None:
        assert isinstance(self._store, LeasedScheduleStore)
        try:
            await self._store.release_occurrences([occurrence])
        except Exception:
            # A release failure leaves the lease to expire naturally. It must
            # not hide the delivery failure or cancellation that caused this
            # cleanup path.
            logging.getLogger(__name__).warning(
                "SchedulerLoop: failed to release occurrence %r",
                occurrence.id,
                exc_info=True,
            )

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
