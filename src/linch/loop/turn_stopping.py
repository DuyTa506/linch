from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from inspect import isawaitable
from typing import TYPE_CHECKING, Literal

from .._telemetry import store_call
from ..events import Event, UserEvent
from ..kernel import Disposable
from ..types import AssistantAssembly, Message, ToolUseBlock
from .checkpoint import (
    _flush_events,
    _message_digest,
    _persist_event,
    _primary_control_message,
    _replayed_feedback_present,
)

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


async def append_feedback(
    session: Session,
    run_id: str,
    decision: TurnStoppingDecision,
) -> AsyncIterator[Event]:
    messages = decision.feedback
    await _flush_events(session)
    if not _replayed_feedback_present(session, messages):
        await store_call(session, "session.append", session.append(list(messages)))
    for message in messages:
        event = UserEvent(message=message, subtype="notification")
        await _persist_event(session, run_id, event)
        yield event


TurnStoppingListener = Callable[
    [TurnStopping], TurnStoppingDecision | None | Awaitable[TurnStoppingDecision | None]
]


class TurnStoppingDispatcher:
    __slots__ = ("_listeners", "replayed_feedback")

    def __init__(self, listeners: tuple[TurnStoppingListener, ...] = ()) -> None:
        self._listeners = [(object(), listener) for listener in listeners]
        self.replayed_feedback: tuple[Message, ...] = ()

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
            if value is None:
                continue
            if isinstance(value, TurnStoppingDecision) and value.action == "accept":
                continue
            if isinstance(value, TurnStoppingDecision):
                if self.replayed_feedback:
                    expected = value.feedback
                    if event.final_block is not None:
                        paired = _primary_control_message(
                            Message(role="assistant", content=list(event.tool_blocks)),
                            "Final tool was not executed.",
                            event.final_block.id,
                        )
                        expected = (paired, *expected)
                    if list(map(_message_digest, self.replayed_feedback)) != list(
                        map(_message_digest, expected)
                    ):
                        raise ValueError("replayed turn feedback differs from its decision")
                    self.replayed_feedback = ()
                return value
            raise TypeError("turn-stopping listeners must return TurnStoppingDecision or None")
        if self.replayed_feedback:
            raise ValueError("unexpected durable turn feedback")
        return TurnStoppingDecision("accept")
