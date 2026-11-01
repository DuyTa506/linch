"""Characterize the deliberately distinct strict and permissive JSON boundaries."""

from __future__ import annotations

import json
from collections import UserDict
from typing import Any

import pytest


def _nested(depth: int, *, mappings: bool = False) -> Any:
    value: Any = "leaf"
    for _ in range(depth):
        value = {"child": value} if mappings else [value]
    return value


def test_strict_json_domains_and_depth_boundaries() -> None:
    from linch.events import _validate_ignorable_json_value
    from linch.model_calls import _copy_json_object, _ModelCallError
    from linch.run_store import _strict_model_json
    from linch.tools.base import normalize_json_value

    assert _strict_model_json(UserDict({"x": [1]})) == {"x": [1]}
    with pytest.raises(ValueError, match=r"\$ contains non-JSON value UserDict"):
        normalize_json_value(UserDict({"x": [1]}))
    _strict_model_json(_nested(100))
    with pytest.raises(TypeError, match="maximum nesting depth 100"):
        _strict_model_json(_nested(101))
    _copy_json_object({"x": _nested(64)})
    with pytest.raises(_ModelCallError, match="bounded, finite JSON without cycles"):
        _copy_json_object({"x": _nested(65)})
    # Tool output and durable inbox metadata share the codec's container-depth
    # limit for both objects and lists.
    for mappings in (False, True):
        normalize_json_value(_nested(100, mappings=mappings))
        with pytest.raises(ValueError, match="maximum nesting depth 100"):
            normalize_json_value(_nested(101, mappings=mappings))
        _validate_ignorable_json_value(_nested(100, mappings=mappings), path="event")
        with pytest.raises(ValueError, match="maximum nesting depth 100"):
            _validate_ignorable_json_value(_nested(101, mappings=mappings), path="event")


def test_journal_exact_scalars_key_error_order_and_deepcopy_aliases() -> None:
    from linch.model_calls import _copy_json_object, _ModelCallError
    from linch.run_store import _strict_model_json
    from linch.tools.base import normalize_json_value

    class IntSubclass(int):
        pass

    number = IntSubclass(1)
    assert _strict_model_json(number) is number
    assert normalize_json_value(number) is number
    with pytest.raises(_ModelCallError, match="bounded, finite JSON without cycles"):
        _copy_json_object({"number": number})
    with pytest.raises(_ModelCallError, match="journal object keys must be strings"):
        _copy_json_object({"earlier": object(), 1: "later"})
    shared = [1]
    original = {"a": shared, "b": shared}
    copied = _copy_json_object(original)
    assert copied["a"] is copied["b"]
    assert copied["a"] is not shared
    normalized = _strict_model_json(original)
    assert normalized["a"] is not normalized["b"]


def test_opaque_and_permissive_event_bytes() -> None:
    from linch.events import _json_safe, event_from_dict, event_to_dict

    raw = {"type": "future", "ignorable": True, "payload": {"z": [1, None], "é": "☃"}}
    assert json.dumps(event_to_dict(event_from_dict(raw)), separators=(",", ":")) == (
        '{"type":"future","ignorable":true,"payload":{"z":[1,null],"\\u00e9":"\\u2603"}}'
    )
    assert json.dumps(_json_safe({1: ("é", b"raw"), "nan": float("nan")})) == (
        '{"1": ["\\u00e9", "b\'raw\'"], "nan": NaN}'
    )
