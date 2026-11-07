"""Event kinds with classes of their own: skills, subagents and tool metadata."""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from typing import get_args

import pytest

from linch_rust import (
    ChildRunStatusEvent,
    CompactionEvent,
    Event,
    InitEvent,
    SkillCompletedEvent,
    SkillInvokedEvent,
    SubagentEvent,
    ToolCallEndEvent,
    UnknownEvent,
    UsageEvent,
)
from linch_rust.events import decode_event

EVENTS_RS = Path(__file__).resolve().parents[2] / "crates/linch-core/src/events"
# Written only to the accounting journal, never streamed to a client.
JOURNAL_ONLY = {"model_call_delivery", "model_call_receipt", "model_call_usage_settlement"}

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
    call = {"tool_use_id": "c1", "ts_ms": 1_700_000_000_123}
    wrapped = decode_event({"type": "subagent", "child_run_id": "r1:c1", **call, "event": END})
    inner = {"type": "subagent", "child_run_id": "r1:c1:c2", "tool_use_id": "c2", "ts_ms": 9}
    nested = decode_event(
        {
            "type": "subagent",
            "child_run_id": "r1:c1",
            **call,
            "event": {**inner, "event": {"type": "x"}},
        }
    )

    assert isinstance(wrapped, SubagentEvent) and wrapped.child_run_id == "r1:c1"
    assert (wrapped.tool_use_id, wrapped.ts_ms) == ("c1", 1_700_000_000_123)
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


def test_a_resumed_init_names_the_turn_it_continues_in() -> None:
    fields = {"type": "init", "session_id": "s1", "run_id": "r1", "model": "m"}
    fields |= {"provider": "scripted", "tools": []}
    first = decode_event(fields)
    again = decode_event({**fields, "resumed": True, "turn": 2})

    assert isinstance(first, InitEvent) and (first.resumed, first.turn) == (False, 0)
    assert isinstance(again, InitEvent) and (again.resumed, again.turn) == (True, 2)


def rust_kinds() -> dict[str, set[str]]:
    """Each streamed Rust event kind's wire name and the names of its fields."""
    event_rs = (EVENTS_RS / "event/mod.rs").read_text(encoding="utf-8")
    kinds_rs = [path.read_text(encoding="utf-8") for path in sorted(EVENTS_RS.glob("kinds/*.rs"))]
    structs = "\n".join([event_rs, *kinds_rs])
    enum = event_rs.split("pub enum Event {", 1)[1].split("\n}", 1)[0]
    payloads = dict(re.findall(r"^\s+(\w+)\((\w+)\),$", enum, re.MULTILINE))
    tags = dict(re.findall(r'Event::(\w+)\(e\) => \{?\s*tagged\("(\w+)", e\)', event_rs))
    tags["Subagent"] = "subagent"
    kinds: dict[str, set[str]] = {}
    for variant, tag in tags.items():
        if tag in JOURNAL_ONLY:
            continue
        found = re.search(rf"pub struct {payloads[variant]} \{{(.*?)\n\}}", structs, re.DOTALL)
        assert found is not None, payloads[variant]
        kinds[tag] = set(re.findall(r"^\s*pub (\w+):", found.group(1), re.MULTILINE))
    return kinds


@pytest.mark.source
def test_every_rust_event_kind_has_a_class_with_its_fields() -> None:
    classes = [cls for cls in get_args(Event) if cls is not UnknownEvent]
    python = {cls.type: {field.name for field in dataclasses.fields(cls)} for cls in classes}
    rust = rust_kinds()

    assert len(rust) > 20, "the Rust sources were read"
    assert python == rust
