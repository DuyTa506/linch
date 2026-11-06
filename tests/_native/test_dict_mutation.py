"""A dict that changes while a native walker iterates it raises RuntimeError.

CPython raises RuntimeError there; the native walkers must not turn it into a
PyO3 panic, which ``except Exception`` cannot catch.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from _linch_native import check_journal_json, deepcopy, json_safe


class _Grower(dict):
    """A dict whose iteration and copy hooks add keys to its parent."""

    def __init__(self, parent: dict[str, Any], *, same_size: bool = False) -> None:
        super().__init__()
        self.parent = parent
        self.same_size = same_size

    def _grow(self) -> None:
        if self.same_size:
            # Drop the visited key and add one: CPython then finds an item
            # past the original length and reports changed keys.
            self.parent.pop("a", None)
            self.parent["c"] = 3
        else:
            self.parent.update({f"k{i}": i for i in range(50)})

    def __iter__(self):  # type: ignore[override]
        self._grow()
        return super().__iter__()

    def items(self):  # type: ignore[override]
        self._grow()
        return super().items()

    def __deepcopy__(self, memo: dict[int, Any]) -> dict[str, Any]:
        self._grow()
        return {}


def _parent(*, same_size: bool = False) -> dict[str, Any]:
    parent: dict[str, Any] = {}
    parent["a"] = _Grower(parent, same_size=same_size)
    parent["b"] = 1
    return parent


WALKERS = {
    "deepcopy": deepcopy,
    "json_safe": lambda value: json_safe(value, strict=False, max_depth=100),
    "journal": check_journal_json,
}


@pytest.mark.parametrize("walker", WALKERS)
def test_size_change_raises_runtime_error(walker: str) -> None:
    with pytest.raises(RuntimeError, match="dictionary changed size during iteration"):
        copy.deepcopy(_parent())
    with pytest.raises(RuntimeError, match="dictionary changed size during iteration"):
        WALKERS[walker](_parent())


@pytest.mark.parametrize("walker", WALKERS)
def test_same_size_key_change_raises_runtime_error(walker: str) -> None:
    with pytest.raises(RuntimeError, match="dictionary keys changed during iteration"):
        copy.deepcopy(_parent(same_size=True))
    with pytest.raises(RuntimeError, match="dictionary keys changed during iteration"):
        WALKERS[walker](_parent(same_size=True))
