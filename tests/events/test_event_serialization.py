"""Event conversion preserves safe tool metadata while breaking cycles."""

from typing import Any

import pytest

from linch.events import ToolCallEndEvent, event_from_dict, event_to_dict
from linch.tools.base import ToolResult


@pytest.mark.parametrize("container", ["dict", "list", "tuple"])
def test_tool_metadata_cycles_are_replaced_on_the_active_path(container: str) -> None:
    metadata: dict[str, Any] = {}
    if container == "dict":
        metadata["cycle"] = metadata
        expected: Any = "<recursion>"
    else:
        children: list[Any] = [metadata]
        metadata["cycle"] = tuple(children) if container == "tuple" else children
        expected = ["<recursion>"]
    shared = {"labels": ["stable"]}
    metadata.update(left=shared, right=shared)
    event = ToolCallEndEvent(
        tool_use_id="tool-1",
        tool_name="Lookup",
        tool_result=ToolResult(content="done", metadata=metadata),
    )

    encoded = event_to_dict(event)

    assert encoded["tool_result"]["metadata"] == {
        "cycle": expected,
        "left": {"labels": ["stable"]},
        "right": {"labels": ["stable"]},
    }
    assert event_to_dict(event_from_dict(encoded)) == encoded
