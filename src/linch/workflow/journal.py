"""Content-addressed journal for workflow resume.

Each ``wf.agent`` call is keyed by ``sha256(subagent_type, prompt,
call_options)`` plus a per-key occurrence counter. ``call_options`` includes
both the tool filter and replay-relevant run options, so a changed tool policy
cannot reuse a result produced under different capabilities. Identical calls
issued in parallel still replay deterministically regardless of completion
order.

There is no journal table: persisted ``WorkflowEvent`` records in the run
store's event log *are* the journal — :meth:`WorkflowJournal.from_stored_events`
folds them back into a lookup on resume.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..events import WorkflowEvent

if TYPE_CHECKING:
    from ..run_store import StoredRunEvent

# Bump only when the call-options fingerprint formula changes (i.e. a change
# that alters call_key for existing calls). A run's version is stamped once,
# in its RunStore meta, at creation (see workflow/engine.py) and then reused
# for that run's entire lifetime — including every resume — so an SDK upgrade
# never invalidates a call_key computed under an earlier formula. Runs
# persisted before this field existed have no version in their meta and
# default to 1 (the original run_options-only formula).
CURRENT_FINGERPRINT_VERSION = 2

# The WorkflowEvent kinds that carry a journaled result. Keep in lockstep with
# WORKFLOW_EVENT_KINDS in events.py — a new kind only replays if it is listed here.
JOURNALED_KINDS = frozenset(
    {
        "agent_end",
        "agent_replayed",
        "step_end",
        "step_replayed",
        "interrupt_resolved",
        "interrupt_replayed",
    }
)


def call_key(subagent_type: str, prompt: str, options_fingerprint: str = "") -> str:
    """Stable content hash identifying one workflow subagent call."""
    if options_fingerprint:
        payload = f"{subagent_type}\x00{prompt}\x00{options_fingerprint}".encode()
    else:
        payload = f"{subagent_type}\x00{prompt}".encode()
    return hashlib.sha256(payload).hexdigest()


def step_key(name: str, key_fingerprint: str = "") -> str:
    """Stable content hash identifying one ``wf.step`` call.

    Domain-separated from :func:`call_key` so a step can never collide with a
    subagent call that happens to share its name.

    Args:
        name: The step's declared name — its durable identity.
        key_fingerprint: Serialized fingerprint of the caller's ``key=``, so the
            same step over different inputs gets distinct journal entries.

    Returns:
        A sha256 hexdigest.
    """
    payload = f"step\x00{name}\x00{key_fingerprint}".encode()
    return hashlib.sha256(payload).hexdigest()


def interrupt_key(key: str) -> str:
    """Stable content hash identifying one ``wf.interrupt`` call.

    Only *key* is hashed — the payload is context for the decider and may be
    recomputed differently across resumes without losing the stored answer.
    """
    return hashlib.sha256(f"interrupt\x00{key}".encode()).hexdigest()


def _dict_or_none(value: Any) -> dict[str, Any] | None:
    return {str(k): v for k, v in value.items()} if isinstance(value, dict) else None


def _record_kind_for(event_kind: str) -> str:
    """Map a journaled ``WorkflowEvent.kind`` back to its record discriminator."""
    prefix, _, _ = event_kind.partition("_")
    return prefix if prefix in {"step", "interrupt"} else "agent"


@dataclass(slots=True)
class WorkflowJournalRecord:
    result_text: str
    structured_output: dict[str, Any] | None = None
    structured_error: str | None = None
    record_kind: str = "agent"
    """``"agent"`` (``result_text`` is the child's final text), or ``"step"`` /
    ``"interrupt"`` (``result_text`` is the JSON encoding of the value)."""


class WorkflowJournal:
    """In-memory result cache keyed by ``(call_key, occurrence)``."""

    def __init__(self, *, fingerprint_version: int = CURRENT_FINGERPRINT_VERSION) -> None:
        self._results: dict[tuple[str, int], WorkflowJournalRecord] = {}
        self._counters: dict[str, int] = {}
        self.fingerprint_version = fingerprint_version

    def next_occurrence(self, key: str) -> int:
        """Return this key's next occurrence index (0-based, monotonic)."""
        occurrence = self._counters.get(key, 0)
        self._counters[key] = occurrence + 1
        return occurrence

    def lookup(self, key: str, occurrence: int) -> str | None:
        record = self.lookup_record(key, occurrence)
        return record.result_text if record is not None else None

    def lookup_record(self, key: str, occurrence: int) -> WorkflowJournalRecord | None:
        return self._results.get((key, occurrence))

    def record(
        self,
        key: str,
        occurrence: int,
        result: str,
        *,
        structured_output: dict[str, Any] | None = None,
        structured_error: str | None = None,
        record_kind: str = "agent",
    ) -> None:
        self._results[(key, occurrence)] = WorkflowJournalRecord(
            result_text=result,
            structured_output=structured_output,
            structured_error=structured_error,
            record_kind=record_kind,
        )

    def snapshot(self) -> list[dict[str, Any]]:
        """Serialize every journaled record as JSON-safe rows.

        Stored in a checkpoint's ``extension_state`` so a resume can seed the
        journal from it and replay only the events appended after it, instead
        of folding the whole event log.

        Returns:
            One row per ``(call_key, occurrence)``; defaulted fields are
            omitted to keep a long run's checkpoint small.
        """
        rows: list[dict[str, Any]] = []
        for (key, occurrence), record in self._results.items():
            row: dict[str, Any] = {
                "key": key,
                "occurrence": occurrence,
                "result_text": record.result_text,
            }
            if record.structured_output is not None:
                row["structured_output"] = record.structured_output
            if record.structured_error is not None:
                row["structured_error"] = record.structured_error
            if record.record_kind != "agent":
                row["record_kind"] = record.record_kind
            rows.append(row)
        return rows

    @classmethod
    def from_stored_events(
        cls,
        events: list[StoredRunEvent],
        *,
        fingerprint_version: int = 1,
        snapshot: list[Any] | None = None,
    ) -> WorkflowJournal:
        """Rebuild the journal from a run's persisted event log.

        Both ``*_end`` (live run) and ``*_replayed`` (a prior resume) records
        fold in, so repeated resumes keep the full prefix cached.

        Args:
            events: The run's stored events, or just the tail after the
                snapshot's watermark.
            fingerprint_version: The formula this run's stored call_keys were
                computed under (from the run's meta; defaults to 1, the
                original formula, for runs persisted before versioning).
            snapshot: Rows from a prior :meth:`snapshot`, folded in first so a
                later stored event still wins. Malformed rows are skipped —
                the event log remains the source of truth.

        Returns:
            A journal holding every record found, in event order.
        """
        journal = cls(fingerprint_version=fingerprint_version)
        for row in snapshot or ():
            if not isinstance(row, dict):
                continue
            key = row.get("key")
            occurrence = row.get("occurrence")
            result_text = row.get("result_text")
            if (
                not isinstance(key, str)
                or not isinstance(occurrence, int)
                or not isinstance(result_text, str)
            ):
                continue
            journal.record(
                key,
                occurrence,
                result_text,
                structured_output=_dict_or_none(row.get("structured_output")),
                structured_error=(
                    row["structured_error"]
                    if isinstance(row.get("structured_error"), str)
                    else None
                ),
                record_kind=str(row.get("record_kind", "agent")),
            )
        for stored in events:
            event = stored.event
            if not isinstance(event, WorkflowEvent):
                continue
            if event.kind not in JOURNALED_KINDS:
                continue
            if event.result_text is None:
                continue
            journal.record(
                event.call_key,
                event.occurrence,
                event.result_text,
                structured_output=event.structured_output,
                structured_error=event.structured_error,
                record_kind=_record_kind_for(event.kind),
            )
        return journal
