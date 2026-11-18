"""The shared compaction fixture is the same file in the Python and linch-rust trees."""

from pathlib import Path

import pytest

NAME = "compaction_request.json"
HERE = Path(__file__).parent / "fixtures" / NAME
RUST = Path(__file__).parents[2] / "linch-rust/crates/linch-core/tests/golden" / NAME


def test_the_shared_fixture_matches_linch_rust() -> None:
    if not RUST.exists():
        pytest.skip("no linch-rust tree next to this one")
    assert HERE.read_bytes() == RUST.read_bytes(), f"copy {RUST} over {HERE} (or the reverse)"
