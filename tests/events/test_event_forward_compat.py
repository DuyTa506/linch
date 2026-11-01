"""Keep unknown-but-ignorable event envelopes forward-compatible."""

from __future__ import annotations

import math
from typing import Any

import pytest

from linch.events import (
    AssistantEvent,
    IgnorableEvent,
    SubagentEvent,
    event_from_dict,
    event_to_dict,
    is_ignorable_event,
)
from linch.ext.reports import build_run_report
from linch.run_store import InMemoryRunStore
from linch.run_stores.postgres import PostgresRunStore
from linch.types import Message


def test_unknown_without_flag_still_raises() -> None:
    with pytest.raises(ValueError, match="unknown event type"):
        event_from_dict({"type": "future_event", "payload": {"k": 1}})


@pytest.mark.parametrize(
    "raw",
    [
        {"ignorable": True},
        {"type": None, "ignorable": True},
        {"type": 1, "ignorable": True},
        {"type": "", "ignorable": True},
        {"type": "future_event", "ignorable": 1},
        {"type": "future_event", "ignorable": "true"},
    ],
)
def test_unknown_event_requires_a_valid_explicitly_ignorable_envelope(
    raw: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        event_from_dict(raw)


@pytest.mark.parametrize(
    "raw",
    [
        {"type": "user", "subtype": "future_subtype"},
        {"type": "assistant", "stop_reason": "future_stop"},
        {"type": "budget", "kind": "future_kind"},
        {"type": "result", "subtype": "future_subtype"},
        {"type": "result", "stop_reason": "future_stop"},
        {"type": "workflow", "kind": "future_kind"},
    ],
)
def test_known_event_rejects_unknown_required_discriminators(raw: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="unknown"):
        event_from_dict(raw)


def test_event_envelope_must_be_an_object() -> None:
    with pytest.raises(ValueError, match="event must be an object"):
        event_from_dict([])  # type: ignore[arg-type]


def test_ignorable_event_round_trips() -> None:
    raw = {"type": "future_event", "ignorable": True, "payload": {"k": 1}}
    event = event_from_dict(raw)
    assert isinstance(event, IgnorableEvent)
    assert is_ignorable_event(event)
    assert event.original_type == "future_event"
    assert event.type == "future_event"
    assert event.raw == raw
    encoded = event_to_dict(event)
    assert encoded == raw
    again = event_from_dict(encoded)
    assert isinstance(again, IgnorableEvent)
    assert again.raw == raw


def test_ignorable_event_isolates_nested_input_and_encoded_output() -> None:
    payload = {"nested": ["stable", {"count": 1}]}
    raw = {"type": "future_event", "ignorable": True, "payload": payload}
    event = event_from_dict(raw)
    assert isinstance(event, IgnorableEvent)

    payload["nested"][1]["count"] = 99
    encoded = event_to_dict(event)
    encoded["payload"]["nested"][1]["count"] = 42

    assert event.raw["payload"]["nested"][1]["count"] == 1
    assert event_to_dict(event)["payload"]["nested"][1]["count"] == 1


@pytest.mark.parametrize(
    "payload",
    [
        {1: "non-string key"},
        {"value": math.nan},
        {"value": math.inf},
        {"value": object()},
    ],
)
def test_ignorable_event_rejects_non_strict_json_payload(payload: object) -> None:
    with pytest.raises(ValueError, match="strictly JSON|only string|NaN|Infinity"):
        IgnorableEvent(
            original_type="future_event",
            raw={"type": "future_event", "ignorable": True, "payload": payload},
        )


def test_ignorable_event_rejects_recursive_payload() -> None:
    payload: list[object] = []
    payload.append(payload)

    with pytest.raises(ValueError, match="reference cycles"):
        IgnorableEvent(
            original_type="future_event",
            raw={"type": "future_event", "ignorable": True, "payload": payload},
        )


def test_ignorable_event_constructor_enforces_round_trip_invariants() -> None:
    raw = {"type": "future_event", "ignorable": True, "payload": {"k": 1}}
    event = IgnorableEvent(original_type="future_event", raw=raw)

    raw["type"] = "mutated_by_caller"
    assert event_to_dict(event)["type"] == "future_event"

    with pytest.raises(ValueError, match="does not match"):
        IgnorableEvent(original_type="other_event", raw=event.raw)
    with pytest.raises(ValueError, match="declare ignorable as true"):
        IgnorableEvent(original_type="future_event", raw={"type": "future_event"})
    with pytest.raises(ValueError, match="known event type"):
        IgnorableEvent(
            original_type="assistant",
            raw={"type": "assistant", "ignorable": True},
        )


def test_ignorable_event_rejects_a_mutated_envelope_when_serialized() -> None:
    event = IgnorableEvent(
        original_type="future_event",
        raw={"type": "future_event", "ignorable": True},
    )
    event.raw["ignorable"] = False

    with pytest.raises(ValueError, match="declare ignorable as true"):
        event_to_dict(event)


def test_ignorable_event_keeps_report_timeline_wire_type() -> None:
    event = event_from_dict({"type": "future_event", "ignorable": True})

    report = build_run_report([event])

    assert report.event_count == 1
    assert report.timeline == [
        {
            "seq": 1,
            "appended_at": None,
            "type": "future_event",
            "event": {"type": "future_event", "ignorable": True},
        }
    ]


async def _assert_store_round_trip(store: InMemoryRunStore | PostgresRunStore) -> None:
    await store.create_run("session-1", id="run-1")
    raw = {"type": "future_event", "ignorable": True, "payload": {"k": 1}}
    assert await store.append_event("run-1", event_from_dict(raw)) == 1

    stored = await store.load_events("run-1")

    assert len(stored) == 1
    assert stored[0].seq == 1
    assert isinstance(stored[0].event, IgnorableEvent)
    assert event_to_dict(stored[0].event) == raw


async def test_ignorable_event_round_trips_through_in_memory_store() -> None:
    await _assert_store_round_trip(InMemoryRunStore())


async def test_ignorable_event_round_trips_through_postgres_store(
    pg_dsn,
) -> None:
    store = PostgresRunStore(pg_dsn)
    try:
        await _assert_store_round_trip(store)
    finally:
        await store.close()


def test_known_event_is_not_ignorable() -> None:
    event = AssistantEvent(message=Message(role="assistant", content=[]), stop_reason="end_turn")
    assert not is_ignorable_event(event)


def test_known_event_type_cannot_claim_ignorable() -> None:
    with pytest.raises(ValueError, match="known event type cannot be wrapped as ignorable"):
        event_from_dict({"type": "system", "subtype": "x", "message": "hi", "ignorable": True})


@pytest.mark.parametrize(
    ("raw", "expected", "error"),
    [
        (
            {"type": "prompt_cache_advisory", "reason": "from_the_future"},
            None,
            "unknown reason",
        ),
        (
            {"type": "prompt_cache_advisory", "detail": "d"},
            "tool_set_changed",
            None,
        ),
        *(
            ({"type": "prompt_cache_advisory", "reason": reason, "detail": "d"}, reason, None)
            for reason in ("tool_set_changed", "model_changed")
        ),
    ],
)
def test_prompt_cache_reason_decoding(
    raw: dict[str, object], expected: str | None, error: str | None
) -> None:
    if error:
        with pytest.raises(ValueError, match=error):
            event_from_dict(raw)
    else:
        event = event_from_dict(raw)
        assert event.reason == expected  # type: ignore[union-attr]


def test_ignorable_event_rejects_nesting_deeper_than_persistence_allows() -> None:
    from linch.events import IGNORABLE_MAX_DEPTH

    payload: Any = "leaf"
    for _ in range(IGNORABLE_MAX_DEPTH + 2):
        payload = [payload]

    with pytest.raises(ValueError, match="nesting depth"):
        IgnorableEvent(
            original_type="future_event",
            raw={"type": "future_event", "ignorable": True, "payload": payload},
        )


def test_ignorable_event_accepts_nesting_at_the_persistence_limit() -> None:
    from linch.events import IGNORABLE_MAX_DEPTH

    payload: Any = "leaf"
    # The event envelope consumes one level; the scalar leaf needs no budget.
    for _ in range(IGNORABLE_MAX_DEPTH - 1):
        payload = [payload]

    event = IgnorableEvent(
        original_type="future_event",
        raw={"type": "future_event", "ignorable": True, "payload": payload},
    )
    assert event_to_dict(event)["payload"] == payload


@pytest.mark.parametrize("kind", ["dict", "list", "mixed"])
def test_ignorable_event_counts_every_container_toward_depth(kind: str) -> None:
    from linch.events import IGNORABLE_MAX_DEPTH

    payload: Any = "leaf"
    for index in range(IGNORABLE_MAX_DEPTH):
        payload = (
            {"next": payload} if kind == "dict" or (kind == "mixed" and index % 2) else [payload]
        )

    with pytest.raises(ValueError, match="nesting depth"):
        IgnorableEvent(
            original_type="future_event",
            raw={"type": "future_event", "ignorable": True, "payload": payload},
        )


@pytest.mark.parametrize("wrappers", [1, 2])
def test_subagent_event_rejects_opaque_payload_overflow_at_construction(wrappers: int) -> None:
    from linch.events import IGNORABLE_MAX_DEPTH

    payload: Any = "leaf"
    for _ in range(IGNORABLE_MAX_DEPTH - wrappers):
        payload = [payload]
    event = IgnorableEvent(
        original_type="future_event",
        raw={"type": "future_event", "ignorable": True, "payload": payload},
    )
    for _ in range(wrappers - 1):
        event = SubagentEvent("parent", "child", "worker", "Worker", event)

    with pytest.raises(ValueError, match="nesting depth"):
        SubagentEvent("parent", "child", "worker", "Worker", event)


def test_subagent_event_revalidates_mutated_opaque_payload_depth() -> None:
    from linch.events import IGNORABLE_MAX_DEPTH

    opaque = IgnorableEvent(
        original_type="future_event", raw={"type": "future_event", "ignorable": True}
    )
    event = SubagentEvent("parent", "child", "worker", "Worker", opaque)
    payload: Any = "leaf"
    for _ in range(IGNORABLE_MAX_DEPTH - 1):
        payload = [payload]
    opaque.raw["payload"] = payload

    with pytest.raises(ValueError, match="nesting depth"):
        event_to_dict(event)


def test_ignorable_depth_limit_matches_the_durable_codec() -> None:
    """The two limits must not drift; that drift is the bug being prevented."""
    from linch.events import IGNORABLE_MAX_DEPTH
    from linch.run_store import _JSON_SAFE_MAX_DEPTH

    assert IGNORABLE_MAX_DEPTH == _JSON_SAFE_MAX_DEPTH
