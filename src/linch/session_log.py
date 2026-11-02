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

import hashlib
from collections.abc import Iterable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Literal

from .durability import _strict_json
from .types import Message, message_to_dict


@dataclass(frozen=True, slots=True)
class MessageEntry:
    """A logged message, optionally excluded from one projection.

    ``visible`` places it in the provider view; ``historical`` places it in the
    full history. A normal message is both; a provider-view-only injection (a
    re-surfaced skill reminder) is ``historical=False``.

    Args:
        message: Message owned by this log entry.
        visible: Whether the message belongs in the provider projection.
        historical: Whether the message belongs in the audit projection.
        kind: Entry discriminator used by the log representation.

    Attributes:
        message: Message owned by this log entry.
        visible: Whether the message belongs in the provider projection.
        historical: Whether the message belongs in the audit projection.
        kind: Entry discriminator used by the log representation.
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

    Args:
        replacement: Messages that become the new provider projection.
        reason: Optional explanation for why the projection was recorded.
        kind: Entry discriminator used by the log representation.

    Attributes:
        replacement: Messages that become the new provider projection.
        reason: Optional explanation for why the projection was recorded.
        kind: Entry discriminator used by the log representation.
    """

    replacement: tuple[Message, ...]
    reason: str = ""
    kind: Literal["projection"] = "projection"


LogEntry = MessageEntry | ProjectionEntry


class SessionLog:
    """Append-only log projecting the provider view and full history."""

    __slots__ = (
        "_entries",
        "_full",
        "_derived",
        "_history_hash",
        "_history_hashed_count",
        "_has_native_definitions",
    )

    def __init__(self) -> None:
        """Initialize an empty append-only session log."""

        self._entries: list[LogEntry] = []
        self._full: list[Message] = []
        self._derived: list[Message] = []
        self._history_hash = None
        self._history_hashed_count = 0
        self._has_native_definitions = False

    def append(self, message: Message, *, visible: bool = True, historical: bool = True) -> None:
        """Append one message, choosing which projections it joins.

        Args:
            message: Message to copy into the authoritative log.
            visible: Whether to include the message in ``provider_view``.
            historical: Whether to include the message in ``full_history``.

        The input message is deep-copied; subsequent direct log reads cannot
        mutate the authoritative projections.
        """

        owned = deepcopy(message)
        self._entries.append(MessageEntry(owned, visible=visible, historical=historical))
        if historical:
            self._full.append(owned)
            if not self._has_native_definitions:
                self._has_native_definitions = any(
                    getattr(block, "tool_definitions", ()) for block in owned.content
                )
        if visible:
            self._derived.append(owned)

    def append_many(self, messages: Iterable[Message]) -> None:
        """Append normal messages to both projections in iterable order.

        Args:
            messages: Messages to append as visible and historical entries.
        """

        for message in messages:
            self.append(message)

    def record_projection(self, replacement: Sequence[Message], reason: str = "") -> bool:
        """Replace the provider view and log the change.

        An equal replacement is a no-op and is not recorded. Returns whether the
        authoritative projection changed.

        Args:
            replacement: Messages that should become the provider projection.
            reason: Optional explanation stored with the projection entry.

        Returns:
            ``True`` when a new projection entry was recorded; otherwise ``False``.
        """
        if list(replacement) == self._derived:
            return False
        owned = deepcopy(list(replacement))
        self._entries.append(ProjectionEntry(tuple(owned), reason))
        self._derived = owned
        return True

    @property
    def entries(self) -> Sequence[LogEntry]:
        """An immutable inspection snapshot of the authoritative entries.

        Entries and their messages are copied so inspection cannot mutate the
        log. Durable session persistence remains message/snapshot based; entries
        are not a durable projection journal.

        Returns:
            A detached tuple containing copies of every log entry.
        """
        return tuple(deepcopy(self._entries))

    @property
    def full_history(self) -> Sequence[Message]:
        """Return a detached snapshot of every historical message.

        Returns:
            A tuple of deep-copied messages in append order.
        """

        return tuple(deepcopy(self._full))

    @property
    def provider_view(self) -> Sequence[Message]:
        """Return a detached snapshot of the current provider view.

        Returns:
            A tuple of deep-copied model-visible messages in projection order.
        """

        return tuple(deepcopy(self._derived))

    @property
    def visible_count(self) -> int:
        """How many messages the provider view holds.

        A count needs no snapshot, so this avoids the whole-history deep copy
        that reading :attr:`provider_view` for its length would cost.

        Returns:
            Number of messages currently visible to the provider.
        """
        return len(self._derived)

    @property
    def history_count(self) -> int:
        """Return the number of messages in full history without copying it.

        Returns:
            Number of historical messages retained by the log.
        """

        return len(self._full)

    def _history_fingerprint(self) -> str:
        """Hash canonical audit-history JSON, folding only its unprocessed tail.

        Keep the array's closing bracket out of the cached prefix so append
        needs no rehash. Serialization stays lazy: arbitrary metadata may be
        appended even when no fingerprint consumer can encode it. A failed tail
        leaves the previously valid prefix and cursor untouched.
        """
        prefix = (
            self._history_hash.copy() if self._history_hash is not None else hashlib.sha256(b"[")
        )
        count = self._history_hashed_count
        while count < len(self._full):
            encoded = _strict_json(
                message_to_dict(self._full[count]), label="model-call input"
            ).encode("utf-8")
            if count:
                prefix.update(b",")
            prefix.update(encoded)
            count += 1
        self._history_hash = prefix
        self._history_hashed_count = count
        complete = prefix.copy()
        complete.update(b"]")
        return complete.hexdigest()

    def last_visible(self) -> Message | None:
        """The newest provider-view message as a detached copy, or ``None``.

        Copies only the returned message rather than the whole projection, for
        callers that just inspect the latest turn.

        Returns:
            A deep-copied newest provider-view message, or ``None`` when empty.
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

        Args:
            historical: Existing audit messages to seed, if any.
            visible: Existing provider projection, if any.

        Returns:
            A new session log initialized from the supplied projections.
        """
        log = cls()
        log.append_many(list(historical or []))
        if visible is not None:
            if list(visible) != log._derived:
                log.record_projection(visible)
        return log
