"""Event kinds with classes of their own: skills, subagents and tool metadata."""

from __future__ import annotations

from linch_rust import (
    ChildRunStatusEvent,
    CompactionEvent,
    InitEvent,
    SkillCompletedEvent,
    SkillInvokedEvent,
    SubagentEvent,
    ToolCallEndEvent,
    UnknownEvent,
    UsageEvent,
)
from linch_rust.events import decode_event

END = {
    "type": "tool_call_end",
    "tool_name": "lookup",
    "result": {"tool_use_id": "c1", "content": [], "is_error": False},
    "duration_ms": 3,
}


def test_a_tool_end_carries_the_tools_metadata() -> None:
    with_metadata = decode_event({**END, "metadata": {"offloaded": "/large/c1.txt"}})
    without = decode_event(END)

    assert isinstance(with_metadata, ToolCallEndEvent)
    assert with_metadata.metadata == {"offloaded": "/large/c1.txt"}
    assert isinstance(without, ToolCallEndEvent) and without.metadata is None


def test_skill_events_decode_to_their_classes() -> None:
    invoked = decode_event({"type": "skill_invoked", "name": "notes", "args": "1.4.0"})
    bare = decode_event({"type": "skill_invoked", "name": "notes"})
    completed = decode_event({"type": "skill_completed", "name": "notes"})
    failed = decode_event({"type": "skill_completed", "name": "notes", "is_error": True})

    assert invoked == SkillInvokedEvent(name="notes", args="1.4.0")
    assert bare == SkillInvokedEvent(name="notes", model_override=None, allowed_tools=None)
    assert completed == SkillCompletedEvent(name="notes", is_error=False)
    assert isinstance(failed, SkillCompletedEvent) and failed.is_error


def test_a_child_run_status_names_the_child_and_its_status() -> None:
    raw = {"type": "child_run_status", "child_run_id": "r1:c1", "status": "running"}
    status = decode_event(raw)

    assert status == ChildRunStatusEvent(child_run_id="r1:c1", status="running")


def test_a_subagent_event_wraps_the_childs_event_decoded() -> None:
    wrapped = decode_event({"type": "subagent", "child_run_id": "r1:c1", "event": END})
    nested = decode_event(
        {
            "type": "subagent",
            "child_run_id": "r1:c1",
            "event": {"type": "subagent", "child_run_id": "r1:c1:c2", "event": {"type": "x"}},
        }
    )

    assert isinstance(wrapped, SubagentEvent) and wrapped.child_run_id == "r1:c1"
    assert isinstance(wrapped.event, ToolCallEndEvent) and wrapped.event.tool_use_id == "c1"
    assert isinstance(nested, SubagentEvent) and isinstance(nested.event, SubagentEvent)
    assert nested.event.event == UnknownEvent("x", {})


def test_the_stream_names_the_provider_the_served_model_and_a_tool_error_code() -> None:
    init = decode_event(
        {"type": "init", "session_id": "s1", "run_id": "r1", "model": "m"}
        | {"provider": "anthropic", "tools": []}
    )
    usage = decode_event(
        {"type": "usage", "usage": {}, "cumulative": {}, "actual_model": "claude-served"}
    )
    end = decode_event({**END, "error_code": "not_found"})
    compaction = decode_event(
        {"type": "compaction", "messages_before": 4, "messages_after": 2}
        | {"tokens_before": 9, "tokens_after": 3, "strategy": "summary", "duration_ms": 12}
    )

    assert isinstance(init, InitEvent) and init.provider == "anthropic"
    assert isinstance(usage, UsageEvent) and usage.actual_model == "claude-served"
    assert isinstance(end, ToolCallEndEvent) and end.error_code == "not_found"
    assert isinstance(compaction, CompactionEvent) and compaction.duration_ms == 12
