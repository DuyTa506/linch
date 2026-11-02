from __future__ import annotations

import pytest

pytest.importorskip("mcp", reason="the 'mcp' extra is not installed")


def test_client_module_imports_against_the_installed_mcp() -> None:
    from linch.mcp import client

    assert callable(client.streamable_http_client)
    assert callable(client.stdio_client)
    assert client.ClientSession is not None


def test_http_transport_takes_a_caller_supplied_client_not_headers() -> None:
    import inspect

    from mcp.client.streamable_http import streamable_http_client

    params = inspect.signature(
        getattr(streamable_http_client, "__wrapped__", streamable_http_client)
    ).parameters
    assert "http_client" in params
    assert "headers" not in params


def test_tool_input_schema_reaches_the_model_intact() -> None:
    from mcp.types import Tool

    from linch.mcp.tool import make_mcp_tool

    schema = {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "what to find"}},
        "required": ["query"],
    }
    mcp_tool = Tool(name="search", description="Search the index.", inputSchema=schema)

    tool = make_mcp_tool("srv", mcp_tool, _unused_call_tool)

    assert tool.input_schema == schema  # type: ignore[attr-defined]


def test_tool_annotations_map_to_scope_and_destructive() -> None:
    from mcp.types import Tool, ToolAnnotations

    from linch.mcp.tool import make_mcp_tool

    read_only = make_mcp_tool(
        "srv",
        Tool(
            name="peek",
            inputSchema={"type": "object", "properties": {}},
            annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
        ),
        _unused_call_tool,
    )
    dangerous = make_mcp_tool(
        "srv",
        Tool(
            name="wipe",
            inputSchema={"type": "object", "properties": {}},
            annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True),
        ),
        _unused_call_tool,
    )

    assert read_only.scope == "read"  # type: ignore[attr-defined]
    assert read_only.parallel is True  # type: ignore[attr-defined]
    assert read_only.destructive is False  # type: ignore[attr-defined]
    assert dangerous.scope == "exec"  # type: ignore[attr-defined]
    assert dangerous.destructive is True  # type: ignore[attr-defined]


@pytest.mark.parametrize("case", ["ok", "error", "image"])
def test_call_tool_result_mapping(case: str) -> None:
    from mcp.types import CallToolResult, ImageContent, TextContent

    from linch.mcp.result import map_mcp_result

    failed = case == "error"
    content = (
        [ImageContent(type="image", data="Zm9v", mimeType="image/jpeg")]
        if case == "image"
        else [TextContent(type="text", text="boom" if failed else "hello")]
    )
    result = map_mcp_result(CallToolResult(content=content, isError=failed))
    assert result.content == ("[image]" if case == "image" else "boom" if failed else "hello")
    assert result.is_error is failed
    if failed:
        assert result.summary.startswith("error: ")


async def _unused_call_tool(name: str, args: dict, signal: object) -> object:
    raise AssertionError("these tests never invoke the tool")
