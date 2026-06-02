from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ..tools import ToolResult
from ..types import Usage


@dataclass(frozen=True, slots=True)
class RunInfo:
    """Passed to ``on_run_start``."""

    run_id: str
    session_id: str
    model: str
    prompt: str
    tools: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TurnInfo:
    """Passed to ``on_turn_start`` and ``on_turn_end``."""

    run_id: str
    turn_index: int


@dataclass(frozen=True, slots=True)
class ProviderCallInfo:
    """Passed to ``on_provider_call_start``."""

    run_id: str
    turn_index: int
    model: str


@dataclass(frozen=True, slots=True)
class ProviderCallResult:
    """Passed to ``on_provider_call_end``."""

    run_id: str
    turn_index: int
    model: str
    stop_reason: str
    usage: Usage
    duration_ms: int


@dataclass(frozen=True, slots=True)
class ToolInfo:
    """Passed to ``on_tool_start``."""

    run_id: str
    turn_index: int
    tool_use_id: str
    tool_name: str
    input: dict[str, Any]
    summary: str


@dataclass(frozen=True, slots=True)
class ToolResultInfo:
    """Passed to ``on_tool_end``."""

    run_id: str
    turn_index: int
    tool_use_id: str
    tool_name: str
    is_error: bool
    duration_ms: int
    result: str = ""
    tool_result: ToolResult | None = None


@dataclass(frozen=True, slots=True)
class RunResultInfo:
    """Passed to ``on_run_end``."""

    run_id: str
    session_id: str
    subtype: str  # "success" | "error" | "aborted"
    stop_reason: str
    total_usage: Usage
    duration_ms: int
    error: dict[str, Any] | None = None


@runtime_checkable
class RunObserver(Protocol):
    """Vendor-neutral observer protocol for Linch run lifecycle spans.

    Every method may be sync or async — the dispatcher awaits the return
    value when it is awaitable, so both plain functions and ``async def``
    methods work without any special marker.

    Implement only the hooks you need; ``BaseObserver`` provides no-op
    defaults so you can subclass rather than implement the full Protocol.
    """

    def on_run_start(self, info: RunInfo) -> Any: ...

    def on_run_end(self, info: RunResultInfo) -> Any: ...

    def on_turn_start(self, info: TurnInfo) -> Any: ...

    def on_turn_end(self, info: TurnInfo) -> Any: ...

    def on_provider_call_start(self, info: ProviderCallInfo) -> Any: ...

    def on_provider_call_end(self, info: ProviderCallResult) -> Any: ...

    def on_tool_start(self, info: ToolInfo) -> Any: ...

    def on_tool_end(self, info: ToolResultInfo) -> Any: ...

    def on_event(self, event: Any) -> Any:
        """Called for every :class:`~linch.events.Event` yielded by the loop."""
        ...


class BaseObserver:
    """No-op base class for observers.

    Subclass this and override only the hooks you care about.  This is
    friendlier than implementing the full :class:`RunObserver` Protocol,
    which requires all nine methods.
    """

    def on_run_start(self, info: RunInfo) -> None:
        pass

    def on_run_end(self, info: RunResultInfo) -> None:
        pass

    def on_turn_start(self, info: TurnInfo) -> None:
        pass

    def on_turn_end(self, info: TurnInfo) -> None:
        pass

    def on_provider_call_start(self, info: ProviderCallInfo) -> None:
        pass

    def on_provider_call_end(self, info: ProviderCallResult) -> None:
        pass

    def on_tool_start(self, info: ToolInfo) -> None:
        pass

    def on_tool_end(self, info: ToolResultInfo) -> None:
        pass

    def on_event(self, event: Any) -> None:
        pass
