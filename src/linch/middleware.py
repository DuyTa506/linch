from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class MiddlewareContext:
    """Provide run and tool identity to a tool-call middleware.

    Attributes:
        deps: Run-scoped dependency object supplied to the tool.

    Args:
        session_id: Session owning the tool call.
        run_id: Run executing the tool call.
        turn_index: Current turn number, when known.
        tool_use_id: Provider identifier for the tool call.
        tool_name: Registered tool name.
        deps: Run-scoped dependencies exposed to the tool.
    """

    session_id: str
    run_id: str
    turn_index: int | None
    tool_use_id: str
    tool_name: str
    deps: Any = None


@dataclass(frozen=True, slots=True)
class ToolCallMiddlewareInput:
    """Describe the input presented to tool-call middleware.

    Attributes:
        input: Mutable-by-copy JSON-compatible arguments that middleware may
            validate or rewrite.

    Args:
        tool_use_id: Provider identifier for the tool call.
        tool_name: Registered tool name.
        input: JSON-compatible arguments presented to the tool.
        summary: Human-readable invocation summary.
    """

    tool_use_id: str
    tool_name: str
    input: dict[str, Any]
    summary: str = ""


@dataclass(frozen=True, slots=True)
class ToolCallMiddlewareResult:
    """Carry middleware input rewrites or a veto error.

    Attributes:
        input: Replacement arguments; ``None`` leaves the original input in
            place when no error is returned.
        error: Non-empty error text vetoes execution.

    Args:
        input: Replacement tool input, when middleware rewrites the call.
        error: Error message that vetoes the call, when present.
    """

    input: dict[str, Any] | None = None
    error: str | None = None


def normalize_middleware(raw: Any) -> list[Any]:
    """Normalize optional middleware configuration to a list.

    Args:
        raw: A middleware object, list or tuple of objects, or ``None``.

    Returns:
        A new list containing the configured middleware objects.
    """

    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return list(raw)
    return [raw]
