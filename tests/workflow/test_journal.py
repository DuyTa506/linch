"""WorkflowJournal unit tests.

linch imports happen inside test functions because tests/loop/test_hardening.py
pops all ``linch*`` modules from ``sys.modules``.
"""

from __future__ import annotations


def test_call_key_stable_and_prompt_sensitive() -> None:
    import hashlib

    from linch.workflow import call_key

    a = call_key("researcher", "find the bug")
    b = call_key("researcher", "find the bug")
    c = call_key("researcher", "find the bugs")
    d = call_key("planner", "find the bug")

    assert a == b
    assert a != c
    assert a != d
    assert len(a) == 64  # sha256 hexdigest
    assert a == hashlib.sha256(b"researcher\x00find the bug").hexdigest()
    assert a != call_key("researcher", "find the bug", "schema=v2")


def test_occurrence_counter_per_key() -> None:
    from linch.workflow import WorkflowJournal, call_key

    journal = WorkflowJournal()
    key_a = call_key("researcher", "task A")
    key_b = call_key("researcher", "task B")

    assert journal.next_occurrence(key_a) == 0
    assert journal.next_occurrence(key_a) == 1
    assert journal.next_occurrence(key_b) == 0


def test_record_and_lookup() -> None:
    from linch.workflow import WorkflowJournal, call_key

    journal = WorkflowJournal()
    key = call_key("researcher", "task A")

    assert journal.lookup(key, 0) is None
    journal.record(key, 0, "result text")
    assert journal.lookup(key, 0) == "result text"
    assert journal.lookup(key, 1) is None


def test_from_stored_events_rebuilds_lookup() -> None:
    from linch.events import AssistantEvent, WorkflowEvent
    from linch.run_store import StoredRunEvent
    from linch.types import Message, TextBlock
    from linch.workflow import WorkflowJournal, call_key

    key = call_key("researcher", "task A")
    stored = [
        StoredRunEvent(
            seq=1,
            appended_at="2026-06-11T00:00:00Z",
            event=WorkflowEvent(kind="phase", title="Research"),
        ),
        StoredRunEvent(
            seq=2,
            appended_at="2026-06-11T00:00:01Z",
            event=AssistantEvent(
                message=Message(role="assistant", content=[TextBlock(text="hi")]),
                stop_reason="end_turn",
            ),
        ),
        StoredRunEvent(
            seq=3,
            appended_at="2026-06-11T00:00:02Z",
            event=WorkflowEvent(
                kind="agent_end",
                call_key=key,
                occurrence=0,
                subagent_type="researcher",
                result_text="finding one",
                structured_output={"finding": 1},
            ),
        ),
        StoredRunEvent(
            seq=4,
            appended_at="2026-06-11T00:00:03Z",
            event=WorkflowEvent(
                kind="agent_replayed",
                call_key=key,
                occurrence=1,
                subagent_type="researcher",
                result_text="finding two",
            ),
        ),
    ]

    journal = WorkflowJournal.from_stored_events(stored)

    # agent_end AND agent_replayed both fold in; other events are ignored.
    assert journal.lookup(key, 0) == "finding one"
    assert journal.lookup(key, 1) == "finding two"
    assert journal.lookup(key, 2) is None
    assert journal.lookup_record(key, 0).structured_output == {"finding": 1}


def test_step_key_is_disjoint_from_call_key() -> None:
    from linch.workflow import call_key
    from linch.workflow.journal import step_key

    # A step named like a subagent type must never collide with its call_key.
    assert step_key("fetch") != call_key("fetch", "")
    assert step_key("fetch") == step_key("fetch")
    assert step_key("fetch") != step_key("fetch", '"url-a"')
    assert len(step_key("fetch")) == 64


def test_from_stored_events_folds_step_records_and_ignores_step_start() -> None:
    from linch.events import WorkflowEvent
    from linch.run_store import StoredRunEvent
    from linch.workflow import WorkflowJournal
    from linch.workflow.journal import step_key

    key = step_key("publish")
    stored = [
        StoredRunEvent(
            seq=1,
            appended_at="2026-06-11T00:00:00Z",
            event=WorkflowEvent(kind="step_start", title="publish", call_key=key, occurrence=0),
        ),
        StoredRunEvent(
            seq=2,
            appended_at="2026-06-11T00:00:01Z",
            event=WorkflowEvent(
                kind="step_end", title="publish", call_key=key, occurrence=0, result_text='"ok"'
            ),
        ),
        StoredRunEvent(
            seq=3,
            appended_at="2026-06-11T00:00:02Z",
            event=WorkflowEvent(
                kind="step_replayed", title="publish", call_key=key, occurrence=1, result_text="42"
            ),
        ),
    ]

    journal = WorkflowJournal.from_stored_events(stored)

    assert journal.lookup(key, 0) == '"ok"'
    assert journal.lookup(key, 1) == "42"
    assert journal.lookup_record(key, 0).record_kind == "step"


def test_snapshot_round_trips_every_record() -> None:
    from linch.workflow import WorkflowJournal, call_key
    from linch.workflow.journal import step_key

    journal = WorkflowJournal()
    agent = call_key("researcher", "task A")
    step = step_key("publish")
    journal.record(agent, 0, "finding", structured_output={"n": 1})
    journal.record(agent, 1, "another")
    journal.record(step, 0, '"ok"', record_kind="step")

    rows = journal.snapshot()
    restored = WorkflowJournal.from_stored_events([], snapshot=rows, fingerprint_version=2)

    assert restored.fingerprint_version == 2
    assert restored.lookup(agent, 0) == "finding"
    assert restored.lookup_record(agent, 0).structured_output == {"n": 1}
    assert restored.lookup(agent, 1) == "another"
    assert restored.lookup_record(step, 0).record_kind == "step"
    # JSON-safe, so it can live in a checkpoint's extension_state.
    import json

    assert json.loads(json.dumps(rows)) == rows


def test_snapshot_is_overridden_by_a_later_stored_event() -> None:
    from linch.events import WorkflowEvent
    from linch.run_store import StoredRunEvent
    from linch.workflow import WorkflowJournal, call_key

    key = call_key("researcher", "task A")
    snapshot = [{"key": key, "occurrence": 0, "result_text": "stale"}]
    stored = [
        StoredRunEvent(
            seq=9,
            appended_at="2026-06-11T00:00:00Z",
            event=WorkflowEvent(kind="agent_end", call_key=key, occurrence=0, result_text="fresh"),
        )
    ]

    journal = WorkflowJournal.from_stored_events(stored, snapshot=snapshot)

    assert journal.lookup(key, 0) == "fresh"


def test_snapshot_ignores_malformed_rows() -> None:
    from linch.workflow import WorkflowJournal

    # A snapshot is opaque extension_state; a hand-edited or truncated one must
    # degrade to "no cached record" rather than break the resume.
    journal = WorkflowJournal.from_stored_events(
        [],
        snapshot=[{"key": "k"}, {"occurrence": 0}, "not a dict", {"key": 1, "occurrence": "x"}],
    )

    assert journal.lookup("k", 0) is None


def test_workflow_event_round_trips_structured_output() -> None:
    from linch.events import WorkflowEvent, event_from_dict, event_to_dict

    event = WorkflowEvent(
        kind="agent_end",
        call_key="key",
        occurrence=0,
        subagent_type="researcher",
        result_text='{"answer":42}',
        structured_output={"answer": 42},
        structured_error=None,
    )

    restored = event_from_dict(event_to_dict(event))

    assert restored == event
