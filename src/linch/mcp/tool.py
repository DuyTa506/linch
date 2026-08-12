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
    mcp_schema: dict[str, Any],
) -> dict[str, Any]:
    """Adapt a server's declared tool schema to the shape a linch tool exposes.

    `Tool.input_schema` is already a JSON Schema dict, so it passes through
    intact — `required`, nested objects, enums and all. Only `type` and
    `properties` are defaulted, for servers that omit them. The dict is copied
    so a tool never aliases the model instance it came from.

    Args:
        mcp_schema: The server's `inputSchema` for one tool.

    Returns:
        A JSON Schema object safe to hand to a provider.
    """
    schema = dict(mcp_schema)
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    return schema


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
    read_only = getattr(annotations, "read_only_hint", False) if annotations is not None else False
    is_destructive = (
        bool(getattr(annotations, "destructive_hint", False)) if annotations is not None else False
    )

    class _McpTool:
        scope = "read" if read_only else "write"
        parallel = read_only
        # Surfaced for the annotation→permission bridge (mcp_permission_rules):
        # a server's destructive_hint maps to an "ask" permission tier.
        destructive = is_destructive

        def __init__(self) -> None:
            self.name = name
            self.description = mcp_tool.description or ""
            self.input_schema = to_input_schema(mcp_tool.input_schema)

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
