"""Session store writes preserve transient-failure recovery semantics."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest


class _FailingSessionStore:
    async def enqueue_inbox(self, _session_id: str, _delivery: Any) -> None:
        raise ConnectionError("session store unavailable")

    async def commit_inbox(self, _session_id: str, *, after_seq: int) -> list[Any]:
        raise AssertionError(f"commit_inbox must not run, got after_seq={after_seq}")

    async def append_messages(self, _session_id: str, _messages: list[Any]) -> list[Any]:
        raise ConnectionError("session store unavailable")

    async def update_meta(self, _session_id: str, _meta: dict[str, object]) -> Any:
        raise ConnectionError("session store unavailable")


def _active_session(*, durable_inbox: bool = False) -> Any:
    from linch.kernel import Context
    from linch.session import Session

    session = Session(
        id="session",
        created_at="now",
        meta={},
        agent=cast(
            Any,
            SimpleNamespace(
                context=Context(),
                run_store=object(),
                durability=SimpleNamespace(durable_inbox=durable_inbox),
            ),
        ),
        store=cast(Any, _FailingSessionStore()),
    )
    session.active_run_id = "run"
    return session


async def test_notify_maps_transient_inbox_write_failure_during_run() -> None:
    from linch.run_store import _StoreUnavailableError
    from linch.types import Message, TextBlock

    session = _active_session(durable_inbox=True)

    with pytest.raises(_StoreUnavailableError) as raised:
        await session.notify(Message(role="user", content=[TextBlock(text="hello")]))

    assert isinstance(raised.value.__cause__, ConnectionError)


async def test_append_maps_transient_message_write_failure_during_run() -> None:
    from linch.run_store import _StoreUnavailableError
    from linch.types import Message, TextBlock

    session = _active_session()

    with pytest.raises(_StoreUnavailableError) as raised:
        await session.append([Message(role="user", content=[TextBlock(text="hello")])])

    assert isinstance(raised.value.__cause__, ConnectionError)
    assert session.message_count == 0
    assert session.session_log.history_count == 0


async def test_update_meta_maps_transient_metadata_write_failure_during_run() -> None:
    from linch.run_store import _StoreUnavailableError

    session = _active_session()

    with pytest.raises(_StoreUnavailableError) as raised:
        await session.update_meta({"key": "value"})

    assert isinstance(raised.value.__cause__, ConnectionError)
    assert session.meta == {}
