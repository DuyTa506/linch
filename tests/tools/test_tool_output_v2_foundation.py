from __future__ import annotations

import math

import pytest
from jsonschema.exceptions import ValidationError


def _ctx():
    from linch.tools import ToolContext

    return ToolContext(cwd=".", session_id="s", run_id="r", session_store=None)


@pytest.mark.parametrize(
    ("value", "rendered"),
    [
        (None, "null"),
        (True, "true"),
        (3, "3"),
        (1.25, "1.25"),
        ("héllo", "héllo"),
        ([3, "x", None], '[3,"x",null]'),
        ({"z": 1, "a": "é"}, '{"a":"é","z":1}'),
    ],
)
def test_strict_json_normalization_and_default_rendering(value, rendered) -> None:
    from linch.tools import ToolOutput, default_render_output, normalize_tool_output

    output = normalize_tool_output(value)
    assert output == ToolOutput(value=value)
    assert default_render_output(output.value) == rendered


@pytest.mark.parametrize(
    "value",
    [
        math.nan,
        math.inf,
        -math.inf,
        {1: "non-string key"},
        ("tuple",),
        object(),
        b"raw bytes",
    ],
)
def test_strict_json_normalizer_rejects_non_json_values(value) -> None:
    from linch.tools import normalize_json_value

    with pytest.raises(ValueError):
        normalize_json_value(value)


def test_strict_json_normalizer_rejects_cycles() -> None:
    from linch.tools import normalize_json_value

    value = []
    value.append(value)
    with pytest.raises(ValueError, match="cycle"):
        normalize_json_value(value)


def test_tool_output_normalizes_attachment_references_without_dropping_them() -> None:
    from linch.tools import ToolAttachment, ToolOutput, normalize_tool_output

    original = ToolOutput(
        value={"ok": True},
        attachments=[
            ToolAttachment(
                reference={"uri": "blob://bucket/key", "generation": 2},
                name="report.json",
                media_type="application/json",
                metadata={"private": False},
            )
        ],
    )
    normalized = normalize_tool_output(original)

    assert normalized == original
    assert normalized is not original
    assert normalized.attachments[0] is not original.attachments[0]


def test_tool_attachment_rejects_raw_bytes_reference() -> None:
    from linch.tools import ToolAttachment, ToolOutput, normalize_tool_output

    with pytest.raises(ValueError, match="bytes"):
        normalize_tool_output(ToolOutput(value="ok", attachments=[ToolAttachment(b"raw")]))


def test_schema_validation_uses_success_value_and_errors_bypass_success_schema() -> None:
    from linch.tools import ToolOutput, ToolOutputError, validate_tool_output

    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "array",
        "prefixItems": [{"const": "ok"}, {"type": "integer"}],
        "items": False,
    }
    validate_tool_output(ToolOutput(["ok", 2]), schema)
    with pytest.raises(ValidationError):
        validate_tool_output(ToolOutput(["bad", 2]), schema)
    validate_tool_output(ToolOutputError("expected miss", code="not_found"), schema)


def test_render_contract_is_sync_and_returns_string() -> None:
    from linch.tools import ToolOutput, render_tool_output

    assert render_tool_output(ToolOutput({"n": 2}), lambda value: f"n={value['n']}") == "n=2"

    with pytest.raises(TypeError, match="return str"):
        render_tool_output(ToolOutput(2), lambda value: value)

    async def async_renderer(value):
        return str(value)

    with pytest.raises(TypeError, match="synchronous"):
        render_tool_output(ToolOutput(2), async_renderer)


async def test_v2_function_tool_preserves_raw_value_and_rejects_tool_result() -> None:
    from linch.tools import ToolResult, tool

    schema = {"type": "object", "required": ["n"], "properties": {"n": {"type": "integer"}}}

    @tool(output_schema=schema)
    def canonical() -> dict[str, int]:
        return {"n": 2}

    @tool(output_schema=schema)
    def invalid_contract() -> ToolResult:
        return ToolResult(content="legacy")

    assert await canonical.execute({}, _ctx()) == {"n": 2}
    with pytest.raises(TypeError, match="must not return ToolResult"):
        await invalid_contract.execute({}, _ctx())


def test_registry_validates_v2_schema_and_renderer_identity() -> None:
    from linch.errors import ConfigError
    from linch.tools import ToolRegistry, tool

    @tool(output_schema={"type": "not-a-json-schema-type"})
    def bad_schema():
        return None

    with pytest.raises(ConfigError, match="output_schema is invalid"):
        ToolRegistry().register(bad_schema)

    @tool(output_schema={"type": "integer"}, render_output=str)
    def anonymous_renderer():
        return 1

    with pytest.raises(ConfigError, match="renderer_id"):
        ToolRegistry().register(anonymous_renderer)

    @tool(
        output_schema={"type": "integer"},
        render_output=lambda value: f"value={value}",
        renderer_id="example.integer",
        renderer_version="1",
    )
    def valid():
        return 1

    registry = ToolRegistry()
    registry.register(valid)
    assert registry.get("valid") is valid


def test_legacy_function_tool_behavior_is_unchanged() -> None:
    from linch.tools import FunctionTool, ToolResult, tool

    @tool
    def legacy() -> dict[str, int]:
        return {"z": 2, "a": 1}

    assert legacy.output_schema is None
    assert legacy.render_output is None
    assert legacy.renderer_id is None
    assert legacy.renderer_version is None

    # Legacy rendering intentionally retains its historical insertion order and spaces.
    import asyncio

    assert asyncio.run(legacy.execute({}, _ctx())) == ToolResult(
        content='{"z": 2, "a": 1}', summary="legacy"
    )

    def positional() -> str:
        return "ok"

    # New V2 fields are appended so existing positional FunctionTool calls keep
    # binding their fifth argument to scope.
    constructed = FunctionTool(positional, "Positional", "desc", None, "write", False)
    assert constructed.scope == "write"
    assert constructed.parallel is False
    assert constructed.output_schema is None


def test_tool_call_end_event_round_trips_canonical_output_and_legacy_projection() -> None:
    from linch.events import ToolCallEndEvent, event_from_dict, event_to_dict
    from linch.tools import ToolAttachment, ToolOutput, ToolResult

    event = ToolCallEndEvent(
        tool_use_id="call-1",
        tool_name="Lookup",
        result='{"id":2}',
        tool_result=ToolResult(content='{"id":2}', summary="Lookup"),
        tool_output=ToolOutput(
            value={"id": 2},
            attachments=[ToolAttachment(reference={"uri": "blob://2"}, name="full.json")],
            metadata={"source": "cache"},
        ),
    )

    raw = event_to_dict(event)
    assert raw["result"] == '{"id":2}'
    assert raw["tool_result"]["content"] == '{"id":2}'
    assert raw["tool_output"]["kind"] == "success"
    assert event_from_dict(raw) == event


def test_tool_call_end_event_round_trips_canonical_error() -> None:
    from linch.events import ToolCallEndEvent, event_from_dict, event_to_dict
    from linch.tools import ToolOutputError

    event = ToolCallEndEvent(
        tool_use_id="call-2",
        tool_name="Lookup",
        result="not found",
        is_error=True,
        tool_output=ToolOutputError(
            message="not found", code="missing", details={"id": 2}, metadata={"retry": False}
        ),
    )
    assert event_from_dict(event_to_dict(event)) == event


def test_tool_call_end_event_keeps_legacy_positional_type_argument() -> None:
    from linch.events import ToolCallEndEvent

    event = ToolCallEndEvent("id", "Name", "ok", False, 1, None, "tool_call_end")
    assert event.type == "tool_call_end"
    assert event.tool_output is None
