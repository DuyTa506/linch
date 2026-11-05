"""Native journal validation must report what the Python walker reports, in order.

The reference is ``linch._json.walk_json`` driven by the journal node check
that ``linch.model_calls`` used before the native port; every case runs through
both and must yield the same first violation (or none).
"""

from __future__ import annotations

import enum
import math
import random
from collections import OrderedDict, defaultdict
from typing import Any

import pytest
from _linch_native import check_journal_json

from linch._json import walk_json

BOUNDED = "journal record must be bounded, finite JSON without cycles"
KEYS = "journal object keys must be strings"


class _Invalid(Exception):
    pass


def _check_node(value: Any, path: str, active: set[int], depth: int) -> int | None:
    if value is None or type(value) in (str, bool, int):
        return None
    if type(value) is float and math.isfinite(value):
        return None
    if not isinstance(value, list | dict) or depth > 64 or id(value) in active:
        raise _Invalid(BOUNDED)
    if isinstance(value, dict) and any(not isinstance(key, str) for key in value):
        raise _Invalid(KEYS)
    return depth + 1


def _reference(value: Any) -> str | None:
    try:
        walk_json(value, check_node=_check_node, copy_containers=False)
    except _Invalid as exc:
        return str(exc)
    return None


class Level(enum.IntEnum):
    LOW = 1


class Text(str):
    pass


class Number(float):
    pass


class Items(list):
    pass


_GOOD = ["", "é☃", 0, -(2**100), True, False, None, 0.0, -0.5, 1e300]
_BAD = [
    float("nan"),
    float("inf"),
    Level.LOW,
    Text("x"),
    Number(1.0),
    (1, 2),
    b"raw",
    object(),
]


def _tree(rng: random.Random, depth: int, bad: float) -> Any:
    if depth > 5 or rng.random() < 0.3:
        return rng.choice(_BAD) if rng.random() < bad else rng.choice(_GOOD)
    if rng.random() < 0.5:
        return [_tree(rng, depth + 1, bad) for _ in range(rng.randrange(4))]
    key: Any = f"k{rng.randrange(9)}"
    if rng.random() < bad:
        key = rng.choice([1, Text("text-subclass"), None])
    return {key: _tree(rng, depth + 1, bad), "v": _tree(rng, depth + 1, bad)}


@pytest.mark.parametrize("seed", range(20))
def test_random_trees_match_python_walker(seed: int) -> None:
    rng = random.Random(seed)
    for _ in range(200):
        value = _tree(rng, 0, bad=rng.choice([0.0, 0.05, 0.3]))
        assert check_journal_json(value) == _reference(value)


def _nested(depth: int) -> Any:
    value: Any = "leaf"
    for _ in range(depth):
        value = [value]
    return value


@pytest.mark.parametrize("depth", range(62, 68))
def test_depth_boundary_matches(depth: int) -> None:
    assert check_journal_json(_nested(depth)) == _reference(_nested(depth))
    assert check_journal_json({"a": _nested(depth)}) == _reference({"a": _nested(depth)})


def test_cycles_are_ancestors_and_shared_siblings_are_not() -> None:
    cycle: list[Any] = [1]
    cycle.append(cycle)
    nested: dict[str, Any] = {"a": {"b": []}}
    nested["a"]["b"].append(nested)
    shared = {"x": [1, 2]}
    for value in (cycle, nested, {"left": shared, "right": shared}, [shared, [shared]]):
        assert check_journal_json(value) == _reference(value)
    assert check_journal_json({"left": shared, "right": shared}) is None


def test_every_key_is_checked_before_children() -> None:
    for value in (
        {"a": float("nan"), 1: "x"},
        [{"a": float("nan")}, {1: 2}],
        {"a": {1: 2}, "b": float("inf")},
        {Text("k"): 1},
    ):
        assert check_journal_json(value) == _reference(value)
    assert check_journal_json({"a": float("nan"), 1: "x"}) == KEYS


def test_container_subclasses_use_their_own_iteration() -> None:
    hooks = defaultdict(list, {"a": [1.5]})
    for value in (
        OrderedDict([("a", 1), ("b", [None])]),
        hooks,
        Items([1, {"k": "v"}]),
        Items([float("nan")]),
        OrderedDict([("a", Level.LOW)]),
    ):
        assert check_journal_json(value) == _reference(value)


def test_subclass_iteration_errors_propagate() -> None:
    class Broken(dict):
        def items(self):  # type: ignore[override]
            raise RuntimeError("items failed")

    with pytest.raises(RuntimeError, match="items failed"):
        check_journal_json(Broken(a=1))
    with pytest.raises(RuntimeError, match="items failed"):
        _reference(Broken(a=1))
