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
    assert metadata["left"] is metadata["right"] is shared
    assert (metadata["cycle"] if container == "dict" else metadata["cycle"][0]) is metadata


@pytest.mark.parametrize("container", ["dict", "list"])
def test_tool_metadata_deep_acyclic_containers_stop_at_the_codec_depth(container: str) -> None:
    from linch.events import IGNORABLE_MAX_DEPTH

    payload: Any = "leaf"
    for _ in range(IGNORABLE_MAX_DEPTH + 1000):
        payload = {"next": payload} if container == "dict" else [payload]
    event = ToolCallEndEvent(
        tool_use_id="tool-1",
        tool_name="Lookup",
        tool_result=ToolResult(content="done", metadata={"payload": payload}),
    )

    encoded = event_to_dict(event)

    node = encoded["tool_result"]["metadata"]["payload"]
    for _ in range(IGNORABLE_MAX_DEPTH - 1):
        node = node["next"] if container == "dict" else node[0]
    assert node == "<max-depth>"


def test_tool_result_omits_attachments_beyond_json_encoder_recursion_limit() -> None:
    payload: Any = "leaf"
    for _ in range(1100):
        payload = [payload]
    event = ToolCallEndEvent(
        tool_use_id="tool-1",
        tool_name="Lookup",
        tool_result=ToolResult(content="done", attachments=[payload]),
    )

    encoded = event_to_dict(event)

    assert "attachments" not in encoded["tool_result"]
    assert event.tool_result.attachments[0] is payload


@pytest.mark.parametrize("boundary", ["citation", "progress"])
def test_event_conversion_cycle_guard_covers_other_metadata_boundaries(boundary: str) -> None:
    from linch.events import ToolProgressEvent
    from linch.tools.base import Citation

    metadata: dict[str, Any] = {"label": "stable"}
    metadata["cycle"] = metadata
    if boundary == "citation":
        event = ToolCallEndEvent(
            tool_use_id="tool-1",
            tool_name="Lookup",
            tool_result=ToolResult(
                content="done",
                citations=[Citation(id="citation-1", source="docs", metadata=metadata)],
            ),
        )
        encoded = event_to_dict(event)["tool_result"]["citations"][0]["metadata"]
    else:
        event = ToolProgressEvent("tool-1", "Lookup", "Working", data=metadata)
        encoded = event_to_dict(event)["data"]

    assert encoded == {"label": "stable", "cycle": "<recursion>"}
    assert metadata["cycle"] is metadata


def test_event_conversion_keeps_supported_legacy_attachments() -> None:
    attachments = [{"reference": "artifact://report", "labels": ["stable"]}]
    event = ToolCallEndEvent(
        tool_use_id="tool-1",
        tool_name="Lookup",
        tool_result=ToolResult(content="done", attachments=attachments),
    )

    encoded = event_to_dict(event)

    assert encoded["tool_result"]["attachments"] == attachments
