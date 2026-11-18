"""Event kinds with classes of their own: skills, subagents and tool metadata."""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path
from typing import get_args

import pytest

from linch_rust import (
    CacheDiagnosticEvent,
    ChildRunStatusEvent,
    CompactionEvent,
    Event,
    InitEvent,
    ModelCallDeliveryEvent,
    ModelCallReceiptEvent,
    ModelCallUsageSettlementEvent,
    ModelFallbackEvent,
    ProviderRetryEvent,
    ScheduleFireEvent,
    SkillCompletedEvent,
    SkillInvokedEvent,
    SubagentEvent,
    ToolCallEndEvent,
    UnknownEvent,
    UsageEvent,
)
from linch_rust.events import decode_event

EVENTS_RS = Path(__file__).resolve().parents[2] / "crates/linch-core/src/events"
# The Rust side's own wire samples: its test decodes and re-encodes each one.
GOLDEN = EVENTS_RS / "golden/accounting.json"

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


def test_reliability_events_decode_to_their_classes() -> None:
    error = {"name": "ProviderError", "kind": "status", "status": 529, "retryable": True}
    retried = decode_event(
        {"type": "provider_retry", "model": "m", "retry": 2, "delay_ms": 2000, "error": error}
    )
    fallback = decode_event(
        {"type": "model_fallback", "from_model": "m", "to_model": "n", "error": error}
    )

    assert retried == ProviderRetryEvent(model="m", retry=2, delay_ms=2000, error=error)
    assert fallback == ModelFallbackEvent(from_model="m", to_model="n", error=error)


def test_a_cache_diagnostic_decodes_with_its_counts_and_its_first_changed_message() -> None:
    wire = {"type": "cache_diagnostic", "reason": "prefix_changed", "previous_cached": 2048}
    wire |= {"cached": 512, "prompt_tokens": 4096, "first_changed_message": 3}
    changed = decode_event(wire)
    unchanged = decode_event({**wire, "reason": "none", "first_changed_message": None})

    assert changed == CacheDiagnosticEvent("prefix_changed", 2048, 512, 4096, 3)
    assert isinstance(unchanged, CacheDiagnosticEvent)
    assert unchanged.reason == "none" and unchanged.first_changed_message is None


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
        | {"measured_tokens": 21, "clipped_bytes": 4096}
    )
    plain = decode_event(
        {"type": "compaction", "messages_before": 4, "messages_after": 2}
        | {"tokens_before": 9, "tokens_after": 3, "strategy": "summary", "duration_ms": 12}
    )

    assert isinstance(init, InitEvent) and init.provider == "anthropic"
    assert isinstance(usage, UsageEvent) and usage.actual_model == "claude-served"
    assert isinstance(end, ToolCallEndEvent) and end.error_code == "not_found"
    assert isinstance(compaction, CompactionEvent) and compaction.duration_ms == 12
    assert compaction.measured_tokens == 21 and compaction.clipped_bytes == 4096
    assert isinstance(plain, CompactionEvent) and plain.clipped_bytes is None


def test_a_resumed_init_names_the_turn_it_continues_in() -> None:
    fields = {"type": "init", "session_id": "s1", "run_id": "r1", "model": "m"}
    fields |= {"provider": "scripted", "tools": []}
    first = decode_event(fields)
    again = decode_event({**fields, "resumed": True, "turn": 2})

    assert isinstance(first, InitEvent) and (first.resumed, first.turn) == (False, 0)
    assert isinstance(again, InitEvent) and (again.resumed, again.turn) == (True, 2)


@pytest.mark.source
def test_the_golden_accounting_samples_decode_to_typed_events() -> None:
    samples = json.loads(GOLDEN.read_text(encoding="utf-8"))
    fire, delivery, receipt, settlement, cancelled = (decode_event(raw) for raw in samples)
    usage = {
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_tokens": 2,
        "cache_creation_tokens": 1,
    }

    assert fire == ScheduleFireEvent(schedule_id="nightly", fire_at_ms=1_762_400_000_000)
    assert delivery == ModelCallDeliveryEvent(
        run_id="r1",
        logical_call_id="r1:primary:0",
        delivery_attempt_id="r1:primary:0:1",
        kind="primary",
        requested_model="claude",
        turn_index=0,
    )
    assert receipt == ModelCallReceiptEvent(
        run_id="r1",
        logical_call_id="r1:primary:0",
        delivery_attempt_id="r1:primary:0:1",
        kind="primary",
        disposition="accepted",
        duration_ms=840,
        actual_model="claude-served",
        usage=usage,
        cost_usd=0.0125,
    )
    assert settlement == ModelCallUsageSettlementEvent(
        run_id="r1",
        logical_call_id="r1:primary:0",
        usage=usage,
        budget_owner_id="root",
        cost_usd=0.0125,
    )
    # Absent members stay None, as the Rust side leaves them off the wire.
    assert isinstance(cancelled, ModelCallReceiptEvent)
    assert (cancelled.actual_model, cancelled.usage, cancelled.cost_usd) == (None, None, None)
    assert (cancelled.kind, cancelled.disposition) == ("compaction", "cancelled")
    # A kind this client does not know stays an UnknownEvent.
    assert decode_event({"type": "model_call_new", "x": 1}) == UnknownEvent(
        "model_call_new", {"x": 1}
    )


def rust_kinds() -> dict[str, set[str]]:
    """Each Rust event kind's wire name and the names of its fields."""
    event_rs = (EVENTS_RS / "event/mod.rs").read_text(encoding="utf-8")
    kinds_rs = [path.read_text(encoding="utf-8") for path in sorted(EVENTS_RS.glob("kinds/*.rs"))]
    model_call_rs = (EVENTS_RS / "model_call.rs").read_text(encoding="utf-8")
    structs = "\n".join([event_rs, *kinds_rs, model_call_rs])
    enum = event_rs.split("pub enum Event {", 1)[1].split("\n}", 1)[0]
    payloads = dict(re.findall(r"^\s+(\w+)\((\w+)\),$", enum, re.MULTILINE))
    tags = dict(re.findall(r'Event::(\w+)\(e\) => \{?\s*tagged\("(\w+)", e\)', event_rs))
    tags["Subagent"] = "subagent"
    kinds: dict[str, set[str]] = {}
    for variant, tag in tags.items():
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
