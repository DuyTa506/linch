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
