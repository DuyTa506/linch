"""Phase 4: the single append-only session log and its two projections."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Literal

import pytest

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


@pytest.mark.parametrize("mode", ["visible_only", "historical_only"])
def test_append_visibility_projection(mode: str) -> None:
    log = SessionLog()
    normal = _msg("normal")
    log.append(normal)
    if mode == "visible_only":
        inject = _msg("reminder")
        log.append(inject, historical=False)
        assert log.full_history == (normal,)
        assert log.provider_view == (normal, inject)
    else:
        audit = _msg("audit")
        log.append(audit, visible=False)
        assert log.full_history == (normal, audit)
        assert log.provider_view == (normal,)


def test_provider_view_is_an_immutable_snapshot() -> None:
    log = SessionLog()
    empty_history = log.full_history
    log.append_many([_msg("1"), _msg("2")])
    assert empty_history == ()
    view = log.provider_view
    history = log.full_history
    with pytest.raises(TypeError):
        view[0] = _msg("escaped")  # type: ignore[index]
    with pytest.raises(TypeError):
        history[0] = _msg("escaped")  # type: ignore[index]
    log.append(_msg("3"))
    assert view == (_msg("1"), _msg("2"))
    assert history == (_msg("1"), _msg("2"))
    for inspected in (log.provider_view[0], log.full_history[0]):
        _text(inspected).text = "tampered"
        assert inspected.provider_metadata is not None
        inspected.provider_metadata["nested"]["source"] = "tampered"
    for authoritative in (log.provider_view[0], log.full_history[0]):
        assert _text(authoritative).text == "1"
        assert authoritative.provider_metadata == {"nested": {"source": "1"}}
    log.record_projection([_msg("summary")])
    assert log.provider_view == (_msg("summary"),)


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


def test_history_fingerprint_defers_cyclic_metadata() -> None:
    from linch.errors import ConfigError

    cycle = []
    cycle.append(cycle)
    message = _msg("cycle")
    message.provider_metadata = {"cycle": cycle}
    log = SessionLog()
    log.append(message)
    with pytest.raises(ConfigError, match="reference cycles"):
        log._history_fingerprint()
    assert log._history_hash is None
    assert log._history_hashed_count == 0
