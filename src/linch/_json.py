"""Shared strict JSON traversal; boundary adapters own acceptance and errors."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

# A node check returns its children's depth, or None for a scalar. This lets
# validation boundaries retain their existing depth counting and error order.
NodeCheck = Callable[[Any, str, set[int], int], int | None]
KeyCheck = Callable[[Any, str], None]


def walk_json(
    value: Any,
    *,
    check_node: NodeCheck,
    check_key: KeyCheck | None = None,
    path: str = "$",
    seen: set[int] | None = None,
    depth: int = 0,
    copy_containers: bool = True,
) -> Any:
    """Visit validated objects/lists once, tracking only active ancestors.

    Validation-only callers retain their own deepcopy semantics, including
    aliases and container subclasses. Normalizers rebuild plain dicts/lists.
    """
    active = seen if seen is not None else set()

    def visit(item: Any, item_path: str, item_depth: int) -> Any:
        child_depth = check_node(item, item_path, active, item_depth)
        if child_depth is None:
            return item
        identity = id(item)
        active.add(identity)
        try:
            if isinstance(item, Mapping):
                result: dict[str, Any] = {}
                for key, child in item.items():
                    if check_key is not None:
                        check_key(key, item_path)
                    normalized = visit(child, f"{item_path}.{key}", child_depth)
                    if copy_containers:
                        result[key] = normalized
                return result if copy_containers else item
            children = []
            for index, child in enumerate(item):
                normalized = visit(child, f"{item_path}[{index}]", child_depth)
                if copy_containers:
                    children.append(normalized)
            return children if copy_containers else item
        finally:
            active.remove(identity)

    return visit(value, path, depth)
