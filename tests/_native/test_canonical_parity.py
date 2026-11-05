"""Native canonical encoding must equal ``json.dumps`` byte-for-byte, or defer."""

from __future__ import annotations

import enum
import json
import random
import sys
from typing import Any

import pytest
from _linch_native import canonical_dumps


def _reference(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )


_TEXT = ["", "a", "é☃", "😀", '"q"', "back\\slash", "\b\f\n\r\t", "\x00\x01\x1f\x7f", " "]
_NUMBERS = [0, -1, 2**63 - 1, 2**63, -(2**100), True, False, None]
_FLOATS = [0.0, -0.0, 0.1, 1.0, 1e16, 1e-7, 123456789.123, 5e-324, sys.float_info.max]


def _tree(rng: random.Random, depth: int) -> Any:
    if depth > 5 or rng.random() < 0.3:
        return rng.choice(_TEXT + _NUMBERS + _FLOATS)
    if rng.random() < 0.5:
        return [_tree(rng, depth + 1) for _ in range(rng.randrange(4))]
    return {rng.choice(_TEXT) + str(i): _tree(rng, depth + 1) for i in range(rng.randrange(5))}


@pytest.mark.parametrize("seed", range(30))
def test_plain_trees_match_json_dumps(seed: int) -> None:
    rng = random.Random(seed)
    for _ in range(100):
        value = _tree(rng, 0)
        assert canonical_dumps(value, max_depth=100) == _reference(value)


class Level(enum.IntEnum):
    LOW = 1


class Text(str):
    pass


def _nested(depth: int) -> Any:
    value: Any = "leaf"
    for _ in range(depth):
        value = [value]
    return value


def test_non_plain_values_defer_to_python() -> None:
    cycle: list[Any] = []
    cycle.append(cycle)
    for value in (
        (1, 2),
        Level.LOW,
        Text("x"),
        {"k": float("nan")},
        [float("inf")],
        cycle,
        "\ud800",
        {1: "int key"},
        {Text("k"): 1},
        object(),
    ):
        assert canonical_dumps({"wrap": value}, max_depth=100) is None


def test_depth_limit_matches_strict_json_boundary() -> None:
    assert canonical_dumps(_nested(100), max_depth=100) == _reference(_nested(100))
    assert canonical_dumps(_nested(101), max_depth=100) is None
    shared = [1]
    assert canonical_dumps({"a": shared, "b": shared}, max_depth=100) == '{"a":[1],"b":[1]}'
