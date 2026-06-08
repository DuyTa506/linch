from __future__ import annotations

import inspect
from dataclasses import dataclass, replace
from typing import Any

from .tools import ToolResult


@dataclass(frozen=True, slots=True)
class MiddlewareContext:
    session_id: str
    run_id: str
    turn_index: int | None
    tool_use_id: str
    tool_name: str
    deps: Any = None

    @property
    def sessionId(self) -> str:
        return self.session_id

    @property
    def runId(self) -> str:
        return self.run_id

    @property
    def turnIndex(self) -> int | None:
        return self.turn_index

    @property
    def toolUseId(self) -> str:
        return self.tool_use_id

    @property
    def toolName(self) -> str:
        return self.tool_name


@dataclass(frozen=True, slots=True)
class ToolCallMiddlewareInput:
    tool_use_id: str
    tool_name: str
    input: dict[str, Any]
    summary: str = ""

    @property
    def toolUseId(self) -> str:
        return self.tool_use_id

    @property
    def toolName(self) -> str:
        return self.tool_name


@dataclass(frozen=True, slots=True)
class ToolCallMiddlewareResult:
    input: dict[str, Any] | None = None
    error: str | None = None


class AgentMiddleware:
    """Tool middleware may implement either hook."""

    def before_tool_call(
        self,
        call: ToolCallMiddlewareInput,
        ctx: MiddlewareContext,
    ) -> ToolCallMiddlewareResult | None:
        return None

    def after_tool_result(
        self,
        call: ToolCallMiddlewareInput,
        result: ToolResult,
        ctx: MiddlewareContext,
    ) -> ToolResult:
        return result


@dataclass(frozen=True, slots=True)
class BeforeToolCallOutcome:
    call: ToolCallMiddlewareInput
    error: str | None = None


def normalize_middleware(raw: Any) -> list[Any]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return list(raw)
    return [raw]


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def dispatch_before_tool_call(
    middleware: list[Any],
    call: ToolCallMiddlewareInput,
    ctx: MiddlewareContext,
) -> BeforeToolCallOutcome:
    current = call
    for item in middleware:
        hook = getattr(item, "before_tool_call", None)
        if hook is None:
            continue
        try:
            raw = await _maybe_await(hook(current, ctx))
        except Exception as exc:
            return BeforeToolCallOutcome(
                call=current,
                error=f"Middleware before_tool_call failed: {exc}",
            )
        if raw is None:
            continue
        if not isinstance(raw, ToolCallMiddlewareResult):
            return BeforeToolCallOutcome(
                call=current,
                error=(
                    "Middleware before_tool_call failed: expected ToolCallMiddlewareResult or None"
                ),
            )
        if raw.input is not None:
            current = replace(current, input=raw.input)
        if raw.error:
            return BeforeToolCallOutcome(call=current, error=raw.error)
    return BeforeToolCallOutcome(call=current)


async def dispatch_after_tool_result(
    middleware: list[Any],
    call: ToolCallMiddlewareInput,
    result: ToolResult,
    ctx: MiddlewareContext,
) -> ToolResult:
    current = result
    for item in middleware:
        hook = getattr(item, "after_tool_result", None)
        if hook is None:
            continue
        try:
            raw = await _maybe_await(hook(call, current, ctx))
        except Exception as exc:
            return ToolResult(
                content=f"Middleware after_tool_result failed: {exc}",
                is_error=True,
                duration_ms=current.duration_ms,
            )
        if not isinstance(raw, ToolResult):
            return ToolResult(
                content="Middleware after_tool_result failed: expected ToolResult",
                is_error=True,
                duration_ms=current.duration_ms,
            )
        current = raw
    return current
