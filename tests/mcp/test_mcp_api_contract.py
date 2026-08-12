"""Pin the parts of the real `mcp` package that `linch.mcp` binds to.

The other tests in this directory inject fake `mcp.*` modules so they can drive
cleanup paths without a server. That makes them blind to upstream API changes:
mcp 2.0 renamed `streamablehttp_client`, dropped its `headers=` kwarg, moved the
transport from a 3-tuple to a 2-tuple yield, and switched the pydantic models to
snake_case fields — and every faked test still passed while master's CI went red.

These tests import the real package and exercise the adapters against real model
instances, so the next such change fails here instead of in a user's process.
"""

from __future__ import annotations

import pytest

pytest.importorskip("mcp", reason="the 'mcp' extra is not installed")


def test_client_module_imports_against_the_installed_mcp() -> None:
    """The transport symbols `client.py` binds at import time still exist."""
    from linch.mcp import client

    assert callable(client.streamable_http_client)
    assert callable(client.stdio_client)
    assert client.ClientSession is not None


def test_http_transport_takes_a_caller_supplied_client_not_headers() -> None:
    """mcp 2.x carries headers on an httpx client, so linch must build one."""
    import inspect

    from mcp.client.streamable_http import streamable_http_client

    params = inspect.signature(
        getattr(streamable_http_client, "__wrapped__", streamable_http_client)
    ).parameters
    assert "http_client" in params
    assert "headers" not in params


def test_tool_input_schema_reaches_the_model_intact() -> None:
    """A server's JSON Schema must survive the adapter, not be flattened away."""
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
    """`read_only_hint` / `destructive_hint` drive the scope and permission tier."""
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
    assert dangerous.scope == "write"  # type: ignore[attr-defined]
    assert dangerous.destructive is True  # type: ignore[attr-defined]


def test_call_tool_result_error_flag_and_text_are_mapped() -> None:
    from mcp.types import CallToolResult, TextContent

    from linch.mcp.result import map_mcp_result

    ok = map_mcp_result(
        CallToolResult(content=[TextContent(type="text", text="hello")], isError=False)
    )
    assert ok.content == "hello"
    assert ok.is_error is False

    failed = map_mcp_result(
        CallToolResult(content=[TextContent(type="text", text="boom")], isError=True)
    )
    assert failed.is_error is True
    assert failed.summary.startswith("error: ")


def test_image_content_media_type_is_read_from_the_model() -> None:
    from mcp.types import CallToolResult, ImageContent

    from linch.mcp.result import map_mcp_result

    result = map_mcp_result(
        CallToolResult(
            content=[ImageContent(type="image", data="Zm9v", mimeType="image/jpeg")],
            isError=False,
        )
    )

    assert result.content == "[image]"
    assert result.is_error is False


async def _unused_call_tool(name: str, args: dict, signal: object) -> object:
    raise AssertionError("these tests never invoke the tool")
