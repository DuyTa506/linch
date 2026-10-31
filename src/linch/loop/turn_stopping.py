"""The single typed, serial veto seam used before terminal turn finalization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from ..kernel import Disposable

TurnStoppingReason = Literal["final_text", "final_tool", "forced_final", "invalid_response"]


@dataclass(frozen=True, slots=True)
class TurnStopping:
    session: Any
    run_id: str
    turn_index: int
    reason: TurnStoppingReason
    assembly: Any = None
    tool_blocks: tuple[Any, ...] = ()
    final_block: Any = None
    forced_final: bool = False
    error_reason: str | None = None


@dataclass(frozen=True, slots=True)
class TurnStoppingDecision:
    action: Literal["accept", "continue", "fail"]
    feedback: tuple[Any, ...] = ()
    error: Exception | None = None


class TurnStoppingDispatcher:
    """Ordered run-local listeners; the first decision is authoritative."""

    __slots__ = ("_listeners",)

    def __init__(self, listeners: tuple[Any, ...] = ()) -> None:
        self._listeners = list(listeners)

    @property
    def active(self) -> bool:
        return bool(self._listeners)

    def register(self, listener: Any) -> Disposable:
        self._listeners.append(listener)
        marker = listener

        def remove() -> None:
            try:
                self._listeners.remove(marker)
            except ValueError:
                pass

        return Disposable(remove)

    async def dispatch(self, event: TurnStopping) -> TurnStoppingDecision:
        for listener in tuple(self._listeners):
            value = listener(event)
            if hasattr(value, "__await__"):
                value = await value
            if value is None or value is False:
                continue
            if isinstance(value, TurnStoppingDecision):
                return value
            if isinstance(value, str) and value == "accept":
                return TurnStoppingDecision("accept")
            raise TypeError("turn-stopping listeners must return TurnStoppingDecision or None")
        return TurnStoppingDecision("accept")
