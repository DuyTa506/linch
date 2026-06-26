"""Tool package extension template.

Use ``@tool`` for ordinary function tools. Reach for the class-shaped protocol
only when you need custom validation, resource declarations, or per-tool knobs
that do not fit the decorator.
"""

from __future__ import annotations

from typing import Any

from linch.tools import ResourceAccess, ToolContext, ToolRegistry, ToolResult, tool
from linch.tools.registry import empty_tools


def make_lookup_tool(values: dict[str, str]):
    """Create the common, decorator-based tool form."""

    @tool(
        name="TemplateLookup",
        description="Look up a value from an application-owned dictionary.",
        scope="read",
        parallel=True,
        summary=lambda input: f"lookup({input['key']})",
        resources=lambda input: [ResourceAccess(f"template:{input['key']}", mode="read")],
    )
    async def lookup_value(key: str, ctx: ToolContext) -> ToolResult:
        del ctx
        value = values.get(key)
        if value is None:
            return ToolResult(content=f"No value for {key}", is_error=True)
        return ToolResult(content=value)

    return lookup_value


class TemplateAdvancedLookupTool:
    """Class-shaped tool for custom validation/resource behavior."""

    name = "TemplateAdvancedLookup"
    description = "Look up a value from an application-owned dictionary."
    input_schema = {
        "type": "object",
        "properties": {"key": {"type": "string"}},
        "required": ["key"],
        "additionalProperties": False,
    }
    scope = "read"
    parallel = True

    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        key = raw.get("key")
        if not isinstance(key, str) or not key:
            raise ValueError("key must be a non-empty string")
        return {"key": key}

    def resources(self, input: dict[str, Any]) -> list[ResourceAccess]:
        return [ResourceAccess(f"template:{input['key']}", mode="read")]

    async def execute(self, input: dict[str, Any], ctx: ToolContext) -> ToolResult:
        del ctx
        key = input["key"]
        value = self._values.get(key)
        if value is None:
            return ToolResult(content=f"No value for {key}", is_error=True)
        return ToolResult(content=value, summary=self.summarize(input))

    def summarize(self, input: dict[str, Any]) -> str:
        return f"lookup({input['key']})"


def build_tools(values: dict[str, str]) -> ToolRegistry:
    """Return a registry containing only this package's tools."""

    return empty_tools(
        make_lookup_tool(values),
        TemplateAdvancedLookupTool(values),
    )
