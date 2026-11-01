"""Minimal duck-typed tools for tests."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any


class StubTool:
    """Return a predictable result from the minimal Linch tool protocol.

    Args:
        name: Model-visible tool name.
        output: Text returned from execution.
        scope: Scheduler scope for the tool.
        parallel: Whether read calls may run concurrently.
        on_execute: Optional callback invoked with input and tool context.
    """

    def __init__(
        self,
        name: str = "Stub",
        *,
        output: str = "ok",
        scope: str = "read",
        parallel: bool = False,
        on_execute: Callable[[dict[str, Any], Any], Any] | None = None,
    ) -> None:
        self.name = name
        self.description = f"{name} test tool"
        self.input_schema: dict[str, Any] = {"type": "object"}
        self.scope = scope
        self.parallel = parallel
        self.output = output
        self.on_execute = on_execute

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Return a shallow copy of validated input."""

        return dict(raw)

    def summarize(self, input: dict[str, Any]) -> str:
        """Return a short execution summary."""

        return self.name

    async def execute(self, input: dict[str, Any], ctx: Any) -> Any:
        """Invoke the optional callback and return a text tool result."""

        if self.on_execute is not None:
            result = self.on_execute(input, ctx)
            if inspect.isawaitable(result):
                await result
        from linch.tools import ToolResult

        return ToolResult(content=self.output, summary=self.name)


def tool_registry(*tools: Any) -> Any:
    """Create a registry containing the supplied test tools.

    Args:
        tools: Duck-typed tool objects to register in insertion order.

    Returns:
        A Linch tool registry ready to pass to an agent.
    """

    from linch.tools import ToolRegistry

    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry
