from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from inspect import isawaitable
from typing import TYPE_CHECKING, Any, Literal

from ..events import Event
from ..kernel import Disposable, EventBus
from ..lifecycle import TURN_STOPPING
from ..types import AssistantAssembly, Message, ToolUseBlock

if TYPE_CHECKING:
    from ..session import Session

TurnStoppingReason = Literal["final_text", "final_tool", "forced_final", "invalid_response"]
INVALID_RESPONSE_DIAGNOSTIC = "[Linch] The provider response was rejected."


@dataclass(frozen=True, slots=True)
class TurnStopping:
    session: Session
    run_id: str
    turn_index: int
    reason: TurnStoppingReason
    assembly: AssistantAssembly | None = None
    tool_blocks: tuple[ToolUseBlock, ...] = ()
    final_block: ToolUseBlock | None = None
    forced_final: bool = False
    error_reason: str | None = None


@dataclass(frozen=True, slots=True)
class TurnStoppingDecision:
    action: Literal["accept", "continue", "fail"]
    feedback: tuple[Message, ...] = ()
    error: Exception | None = None
    event: Event | None = None


TurnStoppingListener = Callable[
    [TurnStopping], TurnStoppingDecision | None | Awaitable[TurnStoppingDecision | None]
]


class TurnStoppingDispatcher:
    __slots__ = ("_bus", "_listeners")

    def __init__(self, listeners: tuple[TurnStoppingListener, ...] = ()) -> None:
        self._bus = EventBus()
        self._listeners: list[tuple[object, TurnStoppingListener, Disposable]] = []
        for listener in listeners:
            self.register(listener)

    @property
    def active(self) -> bool:
        return bool(self._listeners)

    def snapshot(self) -> tuple[TurnStoppingListener, ...]:
        return tuple(listener for _token, listener, _disposer in self._listeners)

    def register(self, listener: TurnStoppingListener) -> Disposable:
        token = object()

        async def wrapped(event: TurnStopping) -> TurnStoppingDecision | None:
            value = listener(event)
            if isawaitable(value):
                value = await value
            if value is not None and not isinstance(value, TurnStoppingDecision):
                raise TypeError("turn-stopping listeners must return TurnStoppingDecision or None")
            return None if value is None or value.action == "accept" else value

        bus_disposer = self._bus.on(TURN_STOPPING, wrapped)
        entry = (token, listener, bus_disposer)
        self._listeners.append(entry)

        def remove() -> Any:
            if entry not in self._listeners:
                return None
            self._listeners.remove(entry)
            return bus_disposer.dispose()

        return Disposable(remove)

    async def dispatch(self, event: TurnStopping) -> TurnStoppingDecision:
        value = await self._bus.serial(TURN_STOPPING, event)
        if value is not None:
            return value
        return TurnStoppingDecision("accept")
