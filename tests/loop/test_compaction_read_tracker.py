from __future__ import annotations

from typing import Any

import pytest


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


@pytest.mark.parametrize("case", ["enabled", "missing", "disabled"])
def test_reset_tracker_modes(case: str) -> None:
    from linch.compaction import CompactionLadder, reset_read_tracker_after_compaction

    session = _session_stub(["/a.py", "/b.py"] if case == "enabled" else ["/a.py"])
    ladder = {
        "enabled": CompactionLadder(),
        "missing": None,
        "disabled": CompactionLadder(reset_read_tracker=False),
    }[case]
    reset_read_tracker_after_compaction(session, _agent_stub(ladder))

    if case == "enabled":
        assert len(session.file_read_tracker) == 0
    else:
        assert "/a.py" in session.file_read_tracker
