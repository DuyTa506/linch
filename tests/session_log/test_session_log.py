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


@pytest.mark.parametrize("many", [False, True])
def test_append_populates_both_projections(many: bool) -> None:
    log = SessionLog()
    msgs = [_msg("a"), _msg("b")] if not many else [_msg("a"), _msg("b"), _msg("c")]
    if many:
        log.append_many(msgs)
    else:
        for message in msgs:
            log.append(message)
    assert log.full_history == tuple(msgs)
    assert log.provider_view == tuple(msgs)


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


def test_equal_projection_is_not_logged() -> None:
    log = SessionLog()
    original = _msg("same")
    log.append(original)

    assert log.record_projection([original], reason="no-op") is False
    assert [entry.kind for entry in log.entries] == ["message"]


@pytest.mark.parametrize(
    ("historical", "visible", "kinds"),
    [
        ([], [], []),
        (["1", "2"], ["1", "2"], ["message", "message"]),
        (["1", "2", "3"], ["summary"], ["message", "message", "message", "projection"]),
        ([], ["parent-a", "parent-b"], ["projection"]),
    ],
)
def test_seed_views_and_projection(
    historical: list[str], visible: list[str], kinds: list[str]
) -> None:
    log = SessionLog.seed(
        historical=[_msg(text) for text in historical],
        visible=[_msg(text) for text in visible],
    )
    assert log.full_history == tuple(_msg(text) for text in historical)
    assert log.provider_view == tuple(_msg(text) for text in visible)
    assert [entry.kind for entry in log.entries] == kinds


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


def test_counts_and_last_visible_are_cheap_accessors() -> None:
    """Count/last-message reads must not copy the whole conversation.

    ``provider_view``/``full_history`` return detached deep copies, so callers
    that only need a length or the newest message would otherwise pay an O(n)
    deepcopy of the entire history on every check.
    """
    log = SessionLog()
    assert log.last_visible() is None
    assert log.visible_count == 0
    log.append_many([_msg("1"), _msg("2")])
    log.append(_msg("audit"), visible=False)

    assert log.visible_count == 2
    assert log.history_count == 3
    assert log.last_visible() == _msg("2")
    first = log.last_visible()
    assert first is not None
    first.content[0].text = "mutated"  # type: ignore[union-attr]
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


def test_history_fingerprint_matches_reference_through_projection_changes() -> None:
    from linch.model_calls import _context_fingerprint
    from linch.types import ToolUseBlock

    log = SessionLog()
    assert log._history_fingerprint() == _context_fingerprint([])
    for index in range(4):
        message = _msg(f"Tiếng Việt {index}")
        message.content.append(
            ToolUseBlock(
                id=f"call-{index}",
                name="search",
                input={"z": [None, True, 1.25], "a": {"nested": "é"}},
            )
        )
        log.append(message, visible=index % 2 == 0)
        assert log._history_fingerprint() == _context_fingerprint(log.full_history)
        before = log._history_fingerprint()
        log.append(_msg("reminder"), historical=False)
        log.record_projection([_msg("summary")])
        assert log._history_fingerprint() == before


def test_history_fingerprint_seed_and_detached_mutations() -> None:
    from linch.model_calls import _context_fingerprint

    historical = [_msg("a"), _msg("b")]
    visible = [_msg("summary")]
    log = SessionLog.seed(historical=historical, visible=visible)
    expected = _context_fingerprint(historical)

    # Mutations before the first demand must not affect lazy serialization.
    _text(historical[0]).text = "changed input"
    assert historical[1].provider_metadata is not None
    historical[1].provider_metadata["nested"]["source"] = "changed input"
    for snapshot in (log.full_history, log.provider_view):
        _text(snapshot[0]).text = "changed snapshot"
        assert snapshot[0].provider_metadata is not None
        snapshot[0].provider_metadata["nested"]["source"] = "changed snapshot"
    entry = log.entries[0]
    assert isinstance(entry, MessageEntry)
    _text(entry.message).text = "changed entry"

    assert log._history_fingerprint() == expected
    inspected = log.full_history[0]
    _text(inspected).text = "changed after fingerprint"
    assert inspected.provider_metadata is not None
    inspected.provider_metadata["nested"]["source"] = "changed after fingerprint"
    assert log._history_fingerprint() == expected
    rebuilt = SessionLog.seed(historical=log.full_history, visible=log.provider_view)
    assert rebuilt._history_fingerprint() == expected
    log.append(_msg("tail"))
    rebuilt.append(_msg("tail"))
    assert rebuilt._history_fingerprint() == log._history_fingerprint()
    assert log._history_fingerprint() == _context_fingerprint(log.full_history)


def test_history_fingerprint_only_serializes_the_demanded_tail(monkeypatch) -> None:
    import linch.types as types

    calls = []
    original = types.message_to_dict

    def counted(message):
        calls.append(message)
        return original(message)

    monkeypatch.setattr(types, "message_to_dict", counted)
    # SessionLog may import the serializer eagerly; cover either import style.
    import linch.session_log as session_log

    monkeypatch.setattr(session_log, "message_to_dict", counted, raising=False)
    hashes = []
    original_sha256 = session_log.hashlib.sha256

    def counted_sha256(*args, **kwargs):
        hashes.append(args)
        return original_sha256(*args, **kwargs)

    monkeypatch.setattr(session_log.hashlib, "sha256", counted_sha256)
    log = SessionLog.seed(historical=[_msg("a"), _msg("b")])
    log.append(_msg("c"))
    log.record_projection([_msg("summary")])
    assert calls == []
    assert hashes == []
    first = log._history_fingerprint()
    assert len(calls) == 3
    assert log._history_fingerprint() == first
    assert len(calls) == 3
    log.append(_msg("injection"), historical=False)
    assert log._history_fingerprint() == first
    assert len(calls) == 3
    log.append(_msg("tail"))
    assert log._history_fingerprint() != first
    assert len(calls) == 4


@pytest.mark.parametrize("invalid", [object(), float("nan"), {1: "bad"}, ("tuple",)])
def test_history_fingerprint_defers_invalid_metadata_and_preserves_prefix(invalid) -> None:
    from linch.errors import ConfigError
    from linch.model_calls import _context_fingerprint

    log = SessionLog()
    log.append(_msg("valid prefix"))
    expected = _context_fingerprint(log.full_history)
    assert log._history_fingerprint() == expected
    prefix = log._history_hash.copy().hexdigest()
    log.append(_msg("valid tail"))
    invalid_message = _msg("invalid tail")
    invalid_message.provider_metadata = {"invalid": invalid}
    log.append(invalid_message)

    visible_only = SessionLog()
    visible_only.append(invalid_message, historical=False)
    visible_only.record_projection([invalid_message])
    assert visible_only._history_fingerprint() == _context_fingerprint([])

    for _ in range(2):
        with pytest.raises(ConfigError, match="model-call input"):
            log._history_fingerprint()
        assert log._history_hash.copy().hexdigest() == prefix
        assert log._history_hashed_count == 1


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


async def test_history_fingerprint_matches_reloaded_session() -> None:
    from linch import Agent
    from linch.model_calls import _context_fingerprint
    from linch.sessions import InMemorySessionStore

    store = InMemorySessionStore()
    original_agent = Agent(model="offline-model", session_store=store)
    original = await original_agent.session(id="history-digest")
    await original.append([_msg("a"), _msg("b")])
    expected = original.session_log._history_fingerprint()

    restarted_agent = Agent(model="offline-model", session_store=store)
    reloaded = await restarted_agent.session(id="history-digest")
    assert reloaded.session_log._history_fingerprint() == expected
    await reloaded.append([_msg("tail")])
    assert reloaded.session_log._history_fingerprint() == _context_fingerprint(
        reloaded.full_history
    )
