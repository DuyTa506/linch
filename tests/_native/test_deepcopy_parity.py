"""The native deepcopy must match ``copy.deepcopy`` value-for-value and alias-for-alias.

The stdlib stays importable, so it is the permanent oracle: every case is
copied by both implementations and the resulting object graphs are compared,
including which nodes are shared with each other and with the original.
"""

from __future__ import annotations

import copy
import datetime
import enum
import random
import threading
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
from _linch_native import deepcopy as native_deepcopy

from linch.types import Message, TextBlock, ToolResultBlock, ToolUseBlock


class Color(enum.Enum):
    RED = 1


class Level(enum.IntEnum):
    LOW = 1


@dataclass(frozen=True)
class Frozen:
    name: str
    items: tuple[Any, ...] = ()


class Plain:
    def __init__(self, value: Any) -> None:
        self.value = value


class Stateful:
    def __init__(self, value: Any) -> None:
        self.value = value

    def __getstate__(self) -> dict[str, Any]:
        return {"wrapped": self.value}

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.value = ("restored", state["wrapped"])


class Hooked:
    def __init__(self, value: Any) -> None:
        self.value = value

    def __deepcopy__(self, memo: dict[int, Any]) -> Hooked:
        return Hooked(("hooked", copy.deepcopy(self.value, memo)))


class Slotted:
    __slots__ = ("a", "b")

    def __init__(self, a: Any, b: Any) -> None:
        self.a, self.b = a, b


class MyList(list[Any]):
    pass


def _message(index: int, shared: Any) -> Message:
    return Message(
        role="assistant",
        content=[
            TextBlock(text=f"text {index}"),
            ToolUseBlock(id=f"call_{index}", name="read", input={"path": "/a", "shared": shared}),
            ToolResultBlock(tool_use_id=f"call_{index}", content=[TextBlock(text="ok")]),
        ],
        provider_metadata={"index": index},
    )


def _scalar(rng: random.Random) -> Any:
    return rng.choice(
        [
            None,
            True,
            7,
            2**80,
            1.5,
            "s",
            b"raw",
            bytearray(b"ba"),
            Decimal("1.1"),
            Color.RED,
            Level.LOW,
            datetime.datetime(2026, 1, 2, 3, 4, 5),
            len,
            _scalar,
            int,
            range(3),
            frozenset({1, 2}),
            Frozen("f"),
        ]
    )


def _value(rng: random.Random, depth: int, pool: list[Any]) -> Any:
    if depth > 4 or rng.random() < 0.25:
        return rng.choice(pool) if pool and rng.random() < 0.3 else _scalar(rng)
    children = [_value(rng, depth + 1, pool) for _ in range(rng.randrange(4))]
    kind = rng.randrange(14)
    if kind == 0:
        built: Any = {f"k{i}": child for i, child in enumerate(children)}
    elif kind == 1:
        built = children
    elif kind == 2:
        built = tuple(children)
    elif kind == 3:
        built = tuple(_scalar(rng) for _ in range(3))
    elif kind == 4:
        built = OrderedDict((str(i), child) for i, child in enumerate(children))
    elif kind == 5:
        built = defaultdict(list, {"d": children})
    elif kind == 6:
        built = Plain(children)
    elif kind == 7:
        built = Stateful(children)
    elif kind == 8:
        built = Hooked(children)
    elif kind == 9:
        built = Slotted(children, rng.choice(pool) if pool else None)
    elif kind == 10:
        built = MyList(children)
    elif kind == 11:
        built = _message(depth, children)
    elif kind == 12:
        built = Frozen("nested", tuple(children))
    else:
        built = {1: children, (1, 2): "tuple-key", Level.LOW: "enum-key"}
    pool.append(built)
    return built


def _add_cycles(value: Any) -> Any:
    loop: list[Any] = [value]
    loop.append(loop)
    holder = Plain(None)
    holder.value = {"self": holder, "loop": loop}
    tuple_cycle: list[Any] = []
    pair = (tuple_cycle, "x")
    tuple_cycle.append(pair)
    return [loop, holder, pair]


def _graph(value: Any, original: Any) -> Any:
    """Describe an object graph by types, values and alias structure."""

    seen: dict[int, int] = {}
    originals = set()

    def collect(node: Any, active: set[int]) -> None:
        if id(node) in active:
            return
        originals.add(id(node))
        for child in _children(node):
            collect(child, active | {id(node)})

    collect(original, set())

    def visit(node: Any) -> Any:
        tag = (type(node).__qualname__, id(node) in originals)
        if id(node) in seen:
            return ("ref", seen[id(node)])
        seen[id(node)] = len(seen)
        children = _children(node)
        if not children:
            return (tag, _leaf(node))
        return (tag, [visit(child) for child in children])

    return visit(value)


def _children(node: Any) -> list[Any]:
    if isinstance(node, dict):
        return [part for item in node.items() for part in item]
    if isinstance(node, list | tuple | frozenset):
        return list(node) if not isinstance(node, frozenset) else sorted(node, key=repr)
    if isinstance(node, Slotted):
        return [node.a, node.b]
    if isinstance(node, Plain | Stateful | Hooked):
        return [node.value]
    if isinstance(node, Message | TextBlock | ToolUseBlock | ToolResultBlock | Frozen):
        return [getattr(node, name) for name in node.__dataclass_fields__]
    return []


def _leaf(node: Any) -> Any:
    return repr(node) if not callable(node) else getattr(node, "__qualname__", repr(node))


def _outcome(fn: Any, value: Any) -> Any:
    try:
        return ("ok", _graph(fn(value), value))
    except Exception as exc:  # parity includes the error type and message
        return ("error", type(exc).__name__, str(exc))


@pytest.mark.parametrize("seed", range(40))
def test_random_graphs_match_stdlib(seed: int) -> None:
    rng = random.Random(seed)
    for _ in range(50):
        value = _value(rng, 0, [])
        if rng.random() < 0.3:
            value = _add_cycles(value)
        assert _outcome(native_deepcopy, value) == _outcome(copy.deepcopy, value)


def test_shared_messages_keep_aliasing_and_detach() -> None:
    shared = {"cache": [1, 2]}
    messages = [_message(index, shared) for index in range(3)]
    copied = native_deepcopy(messages)
    assert copied == messages
    first, second = (copied[i].content[1].input["shared"] for i in (0, 1))
    assert first is second and first is not shared
    assert _graph(copied, messages) == _graph(copy.deepcopy(messages), messages)


def test_immutable_tuples_and_atomics_are_returned_as_is() -> None:
    value = (1, "a", Frozen.__name__, len)
    assert native_deepcopy(value) is value
    assert native_deepcopy((1, [2]))[1] is not None
    assert native_deepcopy(Color.RED) is Color.RED


def test_preseeded_memo_is_honoured_and_extended() -> None:
    inner = [1]
    replacement = ["replaced"]
    memo: dict[int, Any] = {id(inner): replacement}
    assert native_deepcopy({"a": inner}, memo) == {"a": replacement}
    assert native_deepcopy({"a": inner}, memo)["a"] is replacement
    assert id(memo) in memo  # keep-alive list, as in the stdlib


@pytest.mark.parametrize(
    "value",
    [2**40, "memo-key", 1.5, None, len, type],
    ids=["int", "str", "float", "none", "builtin", "type"],
)
def test_preseeded_memo_entry_for_an_atomic_matches_stdlib(value: Any) -> None:
    # 3.14 returns an atomic before reading the memo; 3.11-3.13 read the memo first.
    replacement = object()
    for wrap, unwrap in ((lambda v: v, lambda y: y), (lambda v: [v], lambda y: y[0])):
        expected = unwrap(copy.deepcopy(wrap(value), {id(value): replacement}))
        assert unwrap(native_deepcopy(wrap(value), {id(value): replacement})) is expected


def test_deep_nesting_delegates_without_overflow() -> None:
    value: Any = "leaf"
    for _ in range(400):
        value = [value]
    assert _graph(native_deepcopy(value), value) == _graph(copy.deepcopy(value), value)


def test_uncopyable_values_raise_like_stdlib() -> None:
    lock = {"lock": threading.Lock()}
    assert _outcome(native_deepcopy, lock) == _outcome(copy.deepcopy, lock)
    assert _outcome(native_deepcopy, lock)[0] == "error"


class SlottedChild(Slotted):
    __slots__ = ("c",)


class StatefulBase:
    __slots__ = ("v",)

    def __getstate__(self) -> dict[str, Any]:
        return {"v": ("from-getstate", self.v)}

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.v = state["v"]


class SlottedOverHook(StatefulBase):
    __slots__ = ("w",)


def test_slotted_fast_path_edges_match_stdlib() -> None:
    unset = Slotted.__new__(Slotted)
    unset.a = [1]  # ``b`` stays unset and must stay unset in the copy
    child = SlottedChild([1], {"x": 2})
    child.c = child  # self-reference through an inherited-slot class
    hooked = SlottedOverHook.__new__(SlottedOverHook)
    hooked.v, hooked.w = [3], [4]
    for value in (unset, child, [child, child]):
        assert _outcome(native_deepcopy, value) == _outcome(copy.deepcopy, value)
    assert not hasattr(native_deepcopy(unset), "b")
    # A hook in a base class keeps the stdlib protocol: ``w`` is not in the
    # base's state, so neither implementation copies it.
    expected, copied = copy.deepcopy(hooked), native_deepcopy(hooked)
    assert copied.v == expected.v == ("from-getstate", [3])
    assert not hasattr(copied, "w") and not hasattr(expected, "w")


class BadSlotState:
    def __reduce_ex__(self, protocol: object) -> tuple[Any, ...]:
        return (BadSlotState, (), (None, {1: "bad"}))


def test_non_string_slot_state_key_raises_like_stdlib() -> None:
    assert _outcome(native_deepcopy, BadSlotState()) == _outcome(copy.deepcopy, BadSlotState())
