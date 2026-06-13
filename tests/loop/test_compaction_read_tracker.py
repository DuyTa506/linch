"""Post-compaction read-tracker reset helper (ROADMAP Phase 1.2).

After a compaction elides (micro rung) or summarizes away (forced rung) earlier
messages, the file-read tracker can still claim files were read whose contents
are no longer in context. The ``Edit`` tool gates on that tracker
(``builtin.py`` refuses an edit unless ``has_read``), so a stale entry would let
a *blind* edit proceed on content the model can no longer see. Opting into the
compaction ladder resets the tracker after a compaction so the agent re-reads
before editing — the domain-agnostic generalization of the reference curriculum's
"re-read files after compaction".

These are unit tests for the gate logic of the reset helper; the wiring into the
real loop (proactive + reactive compaction) is covered by integration tests in
``test_compaction_ladder.py``.

linch imports happen inside test functions because sibling tests pop ``linch*``
modules from ``sys.modules``.
"""

from __future__ import annotations

from typing import Any


def _agent_stub(ladder: Any) -> Any:
    class _A:
        compaction_ladder = ladder

    return _A()


def _session_stub(paths: list[str]) -> Any:
    from linch.tools import FileReadTracker

    class _S:
        def __init__(self) -> None:
            self.file_read_tracker = FileReadTracker()

    session = _S()
    for path in paths:
        session.file_read_tracker.add(path)
    return session


def test_reset_clears_tracker_when_ladder_enabled() -> None:
    from linch.compaction import CompactionLadder, reset_read_tracker_after_compaction

    session = _session_stub(["/a.py", "/b.py"])
    reset_read_tracker_after_compaction(session, _agent_stub(CompactionLadder()))

    assert len(session.file_read_tracker) == 0


def test_reset_noop_when_ladder_none() -> None:
    from linch.compaction import reset_read_tracker_after_compaction

    session = _session_stub(["/a.py"])
    reset_read_tracker_after_compaction(session, _agent_stub(None))

    assert "/a.py" in session.file_read_tracker


def test_reset_respects_disable_flag() -> None:
    from linch.compaction import CompactionLadder, reset_read_tracker_after_compaction

    session = _session_stub(["/a.py"])
    ladder = CompactionLadder(reset_read_tracker=False)
    reset_read_tracker_after_compaction(session, _agent_stub(ladder))

    assert "/a.py" in session.file_read_tracker
