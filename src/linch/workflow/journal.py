"""Content-addressed journal for workflow resume.

Each ``wf.agent`` call is keyed by ``sha256(subagent_type, prompt)`` plus a
per-key occurrence counter, so identical calls issued in parallel replay
deterministically regardless of completion order, and an edited prompt
produces a new key (cache miss) without disturbing the rest of the prefix.

There is no journal table: persisted ``WorkflowEvent`` records in the run
store's event log *are* the journal — :meth:`WorkflowJournal.from_stored_events`
folds them back into a lookup on resume.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from ..events import WorkflowEvent

if TYPE_CHECKING:
    from ..run_store import StoredRunEvent


def call_key(subagent_type: str, prompt: str) -> str:
    """Stable content hash identifying one (subagent_type, prompt) call."""
    payload = f"{subagent_type}\x00{prompt}".encode()
    return hashlib.sha256(payload).hexdigest()


class WorkflowJournal:
    """In-memory result cache keyed by ``(call_key, occurrence)``."""

    def __init__(self) -> None:
        self._results: dict[tuple[str, int], str] = {}
        self._counters: dict[str, int] = {}

    def next_occurrence(self, key: str) -> int:
        """Return this key's next occurrence index (0-based, monotonic)."""
        occurrence = self._counters.get(key, 0)
        self._counters[key] = occurrence + 1
        return occurrence

    def lookup(self, key: str, occurrence: int) -> str | None:
        return self._results.get((key, occurrence))

    def record(self, key: str, occurrence: int, result: str) -> None:
        self._results[(key, occurrence)] = result

    @classmethod
    def from_stored_events(cls, events: list[StoredRunEvent]) -> WorkflowJournal:
        """Rebuild the journal from a run's persisted event log.

        Both ``agent_end`` (live run) and ``agent_replayed`` (a prior resume)
        records fold in, so repeated resumes keep the full prefix cached.
        """
        journal = cls()
        for stored in events:
            event = stored.event
            if not isinstance(event, WorkflowEvent):
                continue
            if event.kind not in ("agent_end", "agent_replayed"):
                continue
            if event.result_text is None:
                continue
            journal.record(event.call_key, event.occurrence, event.result_text)
        return journal
