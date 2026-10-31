from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from inspect import isawaitable
from typing import TYPE_CHECKING, Literal

from ..events import Event
from ..kernel import Disposable
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
    __slots__ = ("_listeners",)

    def __init__(self, listeners: tuple[TurnStoppingListener, ...] = ()) -> None:
        self._listeners = [(object(), listener) for listener in listeners]

    @property
    def active(self) -> bool:
        return bool(self._listeners)

    def snapshot(self) -> tuple[TurnStoppingListener, ...]:
        return tuple(listener for _token, listener in self._listeners)

    def register(self, listener: TurnStoppingListener) -> Disposable:
        entry = (object(), listener)
        self._listeners.append(entry)
        return Disposable(
            lambda: self._listeners.remove(entry) if entry in self._listeners else None
        )

    async def dispatch(self, event: TurnStopping) -> TurnStoppingDecision:
        for _token, listener in tuple(self._listeners):
            value = listener(event)
            if isawaitable(value):
                value = await value
            if value is not None and not isinstance(value, TurnStoppingDecision):
                raise TypeError("turn-stopping listeners must return TurnStoppingDecision or None")
            if value is not None and value.action != "accept":
                return value
        return TurnStoppingDecision("accept")
