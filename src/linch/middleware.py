from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class MiddlewareContext:
    session_id: str
    run_id: str
    turn_index: int | None
    tool_use_id: str
    tool_name: str
    deps: Any = None


@dataclass(frozen=True, slots=True)
class ToolCallMiddlewareInput:
    tool_use_id: str
    tool_name: str
    input: dict[str, Any]
    summary: str = ""


@dataclass(frozen=True, slots=True)
class ToolCallMiddlewareResult:
    input: dict[str, Any] | None = None
    error: str | None = None


def normalize_middleware(raw: Any) -> list[Any]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return list(raw)
    return [raw]
