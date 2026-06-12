from __future__ import annotations

import json
from typing import Any

from mcp.types import CallToolResult
from mcp.types import Tool as McpToolDef

from ..errors import AbortError
from ..tools.base import ToolContext, ToolResult
from .naming import build_mcp_tool_name
from .result import map_mcp_result

McpCallTool = Any


def to_input_schema(
    mcp_schema: Any,
) -> dict[str, Any]:
    schema = mcp_schema
    props = getattr(schema, "properties", None) or {}
    required = getattr(schema, "required", None)
    result: dict[str, Any] = {
        "type": "object",
        "properties": props,
    }
    if required:
        result["required"] = list(required)
    return result


def _compact_args(inp: dict[str, Any]) -> str:
    try:
        j = json.dumps(inp)
    except (TypeError, ValueError):
        j = str(inp)
    if not j or j == "{}":
        return ""
    return j[:80] + "…" if len(j) > 80 else j


def make_mcp_tool(
    server_name: str,
    mcp_tool: McpToolDef,
    call_tool: McpCallTool,
) -> object:
    name = build_mcp_tool_name(server_name, mcp_tool.name)
    annotations = getattr(mcp_tool, "annotations", None)
    read_only = getattr(annotations, "readOnlyHint", False) if annotations is not None else False

    class _McpTool:
        scope = "read" if read_only else "write"
        parallel = read_only

        def __init__(self) -> None:
            self.name = name
            self.description = mcp_tool.description or ""
            self.input_schema = to_input_schema(mcp_tool.inputSchema)

        def validate(self, raw: dict[str, object]) -> dict[str, object]:
            if not isinstance(raw, dict) or isinstance(raw, list):
                raise ValueError("MCP tool input must be an object")
            return raw

        def summarize(self, input: dict[str, object]) -> str:
            return f"{name}({_compact_args(input)})"

        async def execute(self, input: dict[str, object], ctx: ToolContext) -> ToolResult:
            from ..abort import throw_if_aborted

            throw_if_aborted(ctx.signal)
            try:
                result: CallToolResult = await call_tool(mcp_tool.name, input, ctx.signal)
                return map_mcp_result(result)
            except AbortError:
                raise
            except Exception as exc:
                throw_if_aborted(ctx.signal)
                return ToolResult(
                    content=f"MCP tool failed: {exc}",
                    summary="mcp error",
                    is_error=True,
                )

    return _McpTool()
