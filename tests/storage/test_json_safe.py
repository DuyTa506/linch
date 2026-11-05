"""Characterize ``run_store._json_safe`` edge cases the native walker must keep."""

from __future__ import annotations

import enum
from collections import OrderedDict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from linch.run_store import _JSON_SAFE_MAX_DEPTH, _json_safe, canonical_json


class Num(enum.IntEnum):
    ONE = 1


class Text(enum.StrEnum):
    A = "a"


class Plain(enum.Enum):
    PAIR = (1, "x")


@dataclass(frozen=True)
class Key:
    n: int


@dataclass
class Holder:
    value: Any
    path: Path


def _nested(depth: int) -> Any:
    value: Any = "leaf"
    for _ in range(depth):
        value = [value]
    return value


def test_int_and_str_enums_pass_through_before_enum_unwrapping() -> None:
    assert _json_safe(Num.ONE) is Num.ONE
    assert _json_safe(Text.A) is Text.A
    assert _json_safe(Plain.PAIR) == [1, "x"]


def test_scalars_and_bytes() -> None:
    assert _json_safe(float("nan")) == "nan"
    assert _json_safe(Decimal("1.10")) == "1.10"
    assert _json_safe(memoryview(b"\x00\xff")) == {"$bytes_base64": "AP8="}
    assert _json_safe(Holder([1], Path("/a"))) == {"value": [1], "path": "/a"}
    assert _json_safe(Holder) == str(Holder)


def test_shared_siblings_are_not_recursion_but_cycles_are() -> None:
    shared = [1]
    assert _json_safe({"a": shared, "b": shared}) == {"a": [1], "b": [1]}
    cycle: list[Any] = [1]
    cycle.append({"self": cycle})
    assert _json_safe(cycle) == [1, {"self": "<recursion>"}]
    with pytest.raises(TypeError, match="contains a recursive value"):
        _json_safe(cycle, strict=True)


def test_non_str_keys_get_type_discriminated_names() -> None:
    value = OrderedDict([("1", "s"), (1, "i"), ("1~int", "r"), (2.0, "f")])
    assert _json_safe(value) == {
        "1": "s",
        "1~int~2": "i",
        "1~int": "r",
        "2.0": "f",
    }
    assert _json_safe({None: 1, Key(2): 2}) == {"None": 1, "Key(n=2)": 2}
    with pytest.raises(TypeError, match="mapping key must be str, got int"):
        _json_safe({1: "x"}, strict=True)


def test_sets_sort_by_canonical_json() -> None:
    assert _json_safe({"b", 2, None, (1, "a")}) == sorted(
        ["b", 2, None, [1, "a"]], key=canonical_json
    )


def test_max_depth_boundary() -> None:
    _json_safe(_nested(_JSON_SAFE_MAX_DEPTH), strict=True)
    with pytest.raises(TypeError, match=f"maximum nesting depth {_JSON_SAFE_MAX_DEPTH}"):
        _json_safe(_nested(_JSON_SAFE_MAX_DEPTH + 1), strict=True)
    truncated = _json_safe(_nested(_JSON_SAFE_MAX_DEPTH + 1))
    for _ in range(_JSON_SAFE_MAX_DEPTH):
        truncated = truncated[0]
    assert truncated == "<max-depth>"


def test_strict_rejects_unstable_values() -> None:
    with pytest.raises(TypeError, match="type object is not deterministically serializable"):
        _json_safe(object(), strict=True)
    assert _json_safe(object()).startswith("<object object")
