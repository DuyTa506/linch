"""The single append-only session log — one source of truth, two projections.

Ported from dsh's session log (deepseek-harness/docs/architecture, "Session log"):
the session-owned provider view is fully represented by the log. Per-request
context assembled by context hooks is deliberately ephemeral and is not part of
this contract. Instead of storing ``provider_view`` and ``full_history`` as two
independently mutated lists, a :class:`SessionLog` holds one ordered list of
entries and *projects* both views from it:

- ``full_history`` — every message entry marked ``historical`` (never compacted).
- ``provider_view`` — every message entry marked
  ``visible``, with each :class:`ProjectionEntry` (a logged compaction) replacing
  the accumulated view by its ``replacement``.

Both projections are materialized eagerly as entries are appended. Inputs are
copied into log-owned message graphs, and public reads return detached immutable
sequences, so neither input owners nor projection readers can mutate the
authoritative state. Compaction is a *logged fact* the log can replay, not an
untracked in-place mutation.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Literal

from .types import Message


@dataclass(frozen=True, slots=True)
class MessageEntry:
    """A logged message, optionally excluded from one projection.

    ``visible`` places it in the provider view; ``historical`` places it in the
    full history. A normal message is both; a provider-view-only injection (a
    re-surfaced skill reminder) is ``historical=False``.
    """

    message: Message
    visible: bool = True
    historical: bool = True
    kind: Literal["message"] = "message"


@dataclass(frozen=True, slots=True)
class ProjectionEntry:
    """A logged compaction: the provider view becomes exactly ``replacement``.

    Subsequent visible message entries append after it. ``full_history`` is
    unaffected — compaction never drops the audit record.
    """

    replacement: tuple[Message, ...]
    reason: str = ""
    kind: Literal["projection"] = "projection"


LogEntry = MessageEntry | ProjectionEntry


class SessionLog:
    """Append-only log projecting the provider view and full history."""

    __slots__ = ("_entries", "_full", "_derived")

    def __init__(self) -> None:
        self._entries: list[LogEntry] = []
        self._full: list[Message] = []
        self._derived: list[Message] = []

    def append(self, message: Message, *, visible: bool = True, historical: bool = True) -> None:
        """Append one message, choosing which projections it joins."""
        owned = deepcopy(message)
        self._entries.append(MessageEntry(owned, visible=visible, historical=historical))
        if historical:
            self._full.append(owned)
        if visible:
            self._derived.append(owned)

    def append_many(self, messages: Iterable[Message]) -> None:
        """Append normal messages (both projections)."""
        for message in messages:
            self.append(message)

    def record_projection(self, replacement: Sequence[Message], reason: str = "") -> bool:
        """Replace the provider view and log the change.

        An equal replacement is a no-op and is not recorded. Returns whether the
        authoritative projection changed.
        """
        projected = list(replacement)
        if projected == self._derived:
            return False
        owned = deepcopy(projected)
        self._entries.append(ProjectionEntry(tuple(owned), reason))
        self._derived = owned
        return True

    @property
    def entries(self) -> Sequence[LogEntry]:
        """An immutable inspection snapshot of the authoritative entries.

        Entries and their messages are copied so inspection cannot mutate the
        log. Durable session persistence remains message/snapshot based; entries
        are not a durable projection journal.
        """
        return tuple(deepcopy(self._entries))

    @property
    def full_history(self) -> Sequence[Message]:
        """A detached, immutable-sequence snapshot of every historical message."""
        return tuple(deepcopy(self._full))

    @property
    def provider_view(self) -> Sequence[Message]:
        """A detached, immutable-sequence snapshot of the current provider view."""
        return tuple(deepcopy(self._derived))

    def derive_messages(self) -> Sequence[Message]:
        """Compatibility alias for :attr:`provider_view`.

        Unlike the former mutable-list API, this never exposes live storage.
        """
        return self.provider_view

    @property
    def visible_count(self) -> int:
        """How many messages the provider view holds.

        A count needs no snapshot, so this avoids the whole-history deep copy
        that reading :attr:`provider_view` for its length would cost.
        """
        return len(self._derived)

    @property
    def history_count(self) -> int:
        """How many messages the full history holds (no snapshot taken)."""
        return len(self._full)

    def last_visible(self) -> Message | None:
        """The newest provider-view message as a detached copy, or ``None``.

        Copies only the returned message rather than the whole projection, for
        callers that just inspect the latest turn.
        """
        if not self._derived:
            return None
        return deepcopy(self._derived[-1])

    @classmethod
    def seed(
        cls,
        *,
        historical: Sequence[Message] | None = None,
        visible: Sequence[Message] | None = None,
    ) -> SessionLog:
        """Build a log from prior state (resume / subagent seed).

        ``historical`` seeds the full history; ``visible`` (when it differs)
        records a projection so the provider view starts compacted. New appends
        and projections extend this log normally.
        """
        log = cls()
        log.append_many(list(historical or []))
        if visible is not None and list(visible) != list(historical or []):
            log.record_projection(visible)
        return log
