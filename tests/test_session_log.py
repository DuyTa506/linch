"""Phase 4: the single append-only session log and its two projections."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from inspect import signature
from typing import Any, Literal, cast

import pytest

from linch.session import Session
from linch.session_log import (
    MessageEntry,
    ProjectionEntry,
    SessionLog,
)
from linch.types import Message, TextBlock


def _msg(text: str, role: Literal["user", "assistant"] = "user") -> Message:
    return Message(
        role=role,
        content=[TextBlock(text=text)],
        provider_metadata={"nested": {"source": text}},
    )


def _text(message: Message) -> TextBlock:
    block = message.content[0]
    assert isinstance(block, TextBlock)
    return block


def test_append_populates_both_projections() -> None:
    log = SessionLog()
    a, b = _msg("a"), _msg("b")
    log.append(a)
    log.append(b)
    assert log.full_history == (a, b)
    assert log.provider_view == (a, b)


def test_append_many_is_both_projections() -> None:
    log = SessionLog()
    msgs = [_msg("a"), _msg("b"), _msg("c")]
    log.append_many(msgs)
    assert log.full_history == tuple(msgs)
    assert log.provider_view == tuple(msgs)


def test_visible_only_entry_skips_full_history() -> None:
    # A re-surfaced skill reminder: provider view only, not audit history.
    log = SessionLog()
    normal = _msg("normal")
    inject = _msg("reminder")
    log.append(normal)
    log.append(inject, historical=False)
    assert log.full_history == (normal,)
    assert log.provider_view == (normal, inject)


def test_historical_only_entry_skips_provider_view() -> None:
    log = SessionLog()
    normal = _msg("normal")
    audit = _msg("audit")
    log.append(normal)
    log.append(audit, visible=False)
    assert log.full_history == (normal, audit)
    assert log.provider_view == (normal,)


def test_projection_replaces_provider_view_not_history() -> None:
    log = SessionLog()
    originals = [_msg("1"), _msg("2"), _msg("3")]
    log.append_many(originals)
    compacted = [_msg("summary")]
    log.record_projection(compacted, reason="compaction")
    assert log.provider_view == tuple(compacted)
    assert log.full_history == tuple(originals)  # audit trail intact


def test_appends_after_projection_extend_compacted_view() -> None:
    log = SessionLog()
    log.append_many([_msg("1"), _msg("2")])
    log.record_projection([_msg("summary")])
    after = _msg("after")
    log.append(after)
    view = log.provider_view
    assert view[-1] == after
    assert view[-1] is not after
    assert len(view) == 2  # summary + after
    assert len(log.full_history) == 3  # 1, 2, after


def test_provider_view_is_an_immutable_snapshot() -> None:
    log = SessionLog()
    log.append_many([_msg("1"), _msg("2")])
    view = log.provider_view
    with pytest.raises(TypeError):
        view[0] = _msg("escaped")  # type: ignore[index]
    log.record_projection([_msg("summary")])
    assert view == (_msg("1"), _msg("2"))
    assert log.provider_view == (_msg("summary"),)


def test_provider_view_nested_mutation_does_not_reach_log() -> None:
    log = SessionLog()
    log.append(_msg("owned"))

    inspected = log.provider_view[0]
    _text(inspected).text = "tampered"
    assert inspected.provider_metadata is not None
    inspected.provider_metadata["nested"]["source"] = "tampered"

    authoritative = log.provider_view[0]
    assert _text(authoritative).text == "owned"
    assert authoritative.provider_metadata == {"nested": {"source": "owned"}}


def test_full_history_is_an_immutable_snapshot() -> None:
    log = SessionLog()
    hist = log.full_history
    log.append(_msg("1"))
    assert hist == ()
    assert log.full_history == (_msg("1"),)
    with pytest.raises(TypeError):
        log.full_history[0] = _msg("escaped")  # type: ignore[index]


def test_full_history_nested_mutation_does_not_reach_log() -> None:
    log = SessionLog()
    log.append(_msg("owned"))

    inspected = log.full_history[0]
    _text(inspected).text = "tampered"
    assert inspected.provider_metadata is not None
    inspected.provider_metadata["nested"]["source"] = "tampered"

    authoritative = log.full_history[0]
    assert _text(authoritative).text == "owned"
    assert authoritative.provider_metadata == {"nested": {"source": "owned"}}


def test_append_takes_defensive_ownership_of_message_graph() -> None:
    log = SessionLog()
    supplied = _msg("input")
    log.append(supplied)

    _text(supplied).text = "tampered"
    assert supplied.provider_metadata is not None
    supplied.provider_metadata["nested"]["source"] = "tampered"

    assert log.provider_view == (_msg("input"),)
    assert log.full_history == (_msg("input"),)


def test_entries_records_order_and_kinds() -> None:
    log = SessionLog()
    log.append(_msg("1"))
    log.record_projection([_msg("s")])
    log.append(_msg("2"))
    entries = log.entries
    assert [e.kind for e in entries] == ["message", "projection", "message"]
    assert isinstance(entries[0], MessageEntry)
    assert isinstance(entries[1], ProjectionEntry)
    with pytest.raises(AttributeError):
        entries.clear()  # type: ignore[attr-defined]


def test_projection_entry_inspection_cannot_mutate_authoritative_log() -> None:
    log = SessionLog()
    summary = _msg("summary")
    log.record_projection([summary], reason="compaction")

    inspected = log.entries[0]
    assert isinstance(inspected, ProjectionEntry)
    with pytest.raises(FrozenInstanceError):
        inspected.reason = "tampered"  # type: ignore[misc]
    inspected.replacement[0].content[0].text = "tampered"  # type: ignore[union-attr]

    authoritative = log.entries[0]
    assert isinstance(authoritative, ProjectionEntry)
    assert authoritative.reason == "compaction"
    assert authoritative.replacement == (_msg("summary"),)


def test_projection_takes_defensive_ownership_of_replacement_graph() -> None:
    log = SessionLog()
    supplied = _msg("summary")
    log.record_projection([supplied], reason="compaction")

    _text(supplied).text = "tampered"
    assert supplied.provider_metadata is not None
    supplied.provider_metadata["nested"]["source"] = "tampered"

    assert log.provider_view == (_msg("summary"),)


def test_equal_projection_is_not_logged() -> None:
    log = SessionLog()
    original = _msg("same")
    log.append(original)

    assert log.record_projection([original], reason="no-op") is False
    assert [entry.kind for entry in log.entries] == ["message"]


def test_session_constructor_legacy_views_seed_canonical_log() -> None:
    parameters = signature(Session).parameters
    assert "provider_view" in parameters
    assert "full_history" in parameters

    provider = [_msg("summary")]
    history = [_msg("original")]
    constructor = cast(Any, Session)
    with pytest.warns(DeprecationWarning, match="session_log=SessionLog.seed"):
        session = constructor(
            id="legacy",
            created_at="now",
            meta={},
            agent=object(),
            store=object(),
            provider_view=provider,
            full_history=history,
        )

    _text(provider[0]).text = "tampered-provider"
    _text(history[0]).text = "tampered-history"
    assert session.provider_view == (_msg("summary"),)
    assert session.full_history == (_msg("original"),)


def test_seed_equal_views_no_projection() -> None:
    hist = [_msg("1"), _msg("2")]
    log = SessionLog.seed(historical=hist, visible=list(hist))
    assert log.full_history == tuple(hist)
    assert log.provider_view == tuple(hist)
    assert all(e.kind == "message" for e in log.entries)


def test_seed_differing_views_records_projection() -> None:
    hist = [_msg("1"), _msg("2"), _msg("3")]
    view = [_msg("summary")]
    log = SessionLog.seed(historical=hist, visible=view)
    assert log.full_history == tuple(hist)
    assert log.provider_view == tuple(view)
    assert log.entries[-1].kind == "projection"


def test_seed_takes_defensive_ownership_of_both_input_graphs() -> None:
    historical = _msg("history")
    visible = _msg("summary")
    log = SessionLog.seed(historical=[historical], visible=[visible])

    _text(historical).text = "tampered-history"
    _text(visible).text = "tampered-summary"
    assert historical.provider_metadata is not None
    historical.provider_metadata["nested"]["source"] = "tampered"
    assert visible.provider_metadata is not None
    visible.provider_metadata["nested"]["source"] = "tampered"

    assert log.full_history == (_msg("history"),)
    assert log.provider_view == (_msg("summary"),)


def test_seed_visible_only_subagent_case() -> None:
    # Subagent seeds provider view from parent; its own history starts empty.
    seed_view = [_msg("parent-a"), _msg("parent-b")]
    log = SessionLog.seed(historical=[], visible=seed_view)
    assert log.full_history == ()
    assert log.provider_view == tuple(seed_view)


def test_seed_empty_is_empty() -> None:
    log = SessionLog.seed()
    assert log.full_history == ()
    assert log.provider_view == ()


def test_session_provider_projection_is_logged() -> None:
    # Every message in the session-owned provider projection is traceable to a
    # logged entry. Ephemeral request context is intentionally outside this log.
    log = SessionLog()
    log.append_many([_msg("1"), _msg("2")])
    log.append(_msg("reminder"), historical=False)
    summary = _msg("summary")
    log.record_projection([summary, _msg("kept")])
    log.append(_msg("after"))

    logged: list[Message] = []
    for entry in log.entries:
        if isinstance(entry, MessageEntry) and entry.visible:
            logged.append(entry.message)
        elif isinstance(entry, ProjectionEntry):
            logged.extend(entry.replacement)
    for message in log.provider_view:
        assert message in logged


def test_counts_and_last_visible_are_cheap_accessors() -> None:
    """Count/last-message reads must not copy the whole conversation.

    ``provider_view``/``full_history`` return detached deep copies, so callers
    that only need a length or the newest message would otherwise pay an O(n)
    deepcopy of the entire history on every check.
    """
    log = SessionLog()
    log.append_many([_msg("1"), _msg("2")])
    log.append(_msg("audit"), visible=False)

    assert log.visible_count == 2
    assert log.history_count == 3
    assert log.last_visible() == _msg("2")

    import linch.session_log as mod

    calls = 0
    real = mod.deepcopy

    def counting(obj):
        nonlocal calls
        calls += 1
        return real(obj)

    mod.deepcopy = counting
    try:
        _ = log.visible_count
        _ = log.history_count
        assert calls == 0, "counts must not deepcopy"
        _ = log.last_visible()
        assert calls == 1, "last_visible copies only the one message it returns"
    finally:
        mod.deepcopy = real


def test_last_visible_is_detached_and_empty_safe() -> None:
    log = SessionLog()
    assert log.last_visible() is None
    assert log.visible_count == 0

    log.append(_msg("only"))
    first = log.last_visible()
    assert first is not None
    first.content[0].text = "mutated"  # type: ignore[union-attr]
    assert log.last_visible() == _msg("only")  # authoritative state untouched
