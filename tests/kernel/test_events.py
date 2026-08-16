from __future__ import annotations

import asyncio

import pytest

from linch.kernel import EventBus


@pytest.mark.asyncio
async def test_emit_isolates_throwing_listener() -> None:
    seen: list[str] = []

    def boom(_payload: object) -> None:
        raise RuntimeError("listener failed")

    def ok(payload: object) -> None:
        seen.append(f"ok:{payload}")

    bus = EventBus()
    bus.on("evt", boom)
    bus.on("evt", ok)
    bus.emit("evt", "x")  # non-vetoing, must not raise
    assert seen == ["ok:x"]


@pytest.mark.asyncio
async def test_serial_bails_on_first_truthy() -> None:
    bus = EventBus()
    bus.on("evt", lambda _p: None)
    bus.on("evt", lambda _p: "stop")
    bus.on("evt", lambda _p: "never")
    result = await bus.serial("evt", "x")
    assert result == "stop"


@pytest.mark.asyncio
async def test_waterfall_composes_outer_to_inner() -> None:
    order: list[str] = []

    async def outer(payload: dict, next) -> object:
        order.append("outer:before")
        result = await next()
        order.append("outer:after")
        return result

    async def inner(payload: dict, next) -> object:
        order.append("inner:before")
        result = await next()
        order.append("inner:after")
        return result

    bus = EventBus()
    bus.on("wf", outer)
    bus.on("wf", inner)

    async def terminal() -> str:
        order.append("terminal")
        return "done"

    result = await bus.waterfall("wf", {}, next=terminal)
    assert result == "done"
    assert order == [
        "outer:before",
        "inner:before",
        "terminal",
        "inner:after",
        "outer:after",
    ]


@pytest.mark.asyncio
async def test_waterfall_listener_can_veto_by_not_calling_next() -> None:
    reached: list[str] = []

    async def veto(_payload: dict, next) -> str:
        return "vetoed"

    async def never(_payload: dict, next) -> object:
        reached.append("never")
        return await next()

    bus = EventBus()
    bus.on("wf", veto)
    bus.on("wf", never)

    async def terminal() -> str:
        reached.append("terminal")
        return "done"

    result = await bus.waterfall("wf", {}, next=terminal)
    assert result == "vetoed"
    assert reached == []


@pytest.mark.asyncio
async def test_waterfall_terminal_runs_with_no_listeners() -> None:
    bus = EventBus()

    async def terminal() -> str:
        return "terminal-only"

    assert await bus.waterfall("wf", {}, next=terminal) == "terminal-only"


@pytest.mark.asyncio
async def test_waterfall_listener_mutates_payload_in_place() -> None:
    async def mutate(payload: dict, next) -> object:
        payload["seen"] = True
        return await next()

    bus = EventBus()
    bus.on("wf", mutate)
    payload: dict = {}

    async def terminal() -> dict:
        return payload

    await bus.waterfall("wf", payload, next=terminal)
    assert payload["seen"] is True


@pytest.mark.asyncio
async def test_on_returns_disposer_that_unregisters() -> None:
    seen: list[str] = []
    bus = EventBus()
    handle = bus.on("evt", lambda p: seen.append(f"a:{p}"))
    bus.emit("evt", "1")
    await handle.dispose()
    bus.emit("evt", "2")
    assert seen == ["a:1"]


@pytest.mark.asyncio
async def test_on_prepend_orders_before_existing() -> None:
    order: list[str] = []
    bus = EventBus()
    bus.on("evt", lambda _p: order.append("second"))
    bus.on("evt", lambda _p: order.append("first"), prepend=True)
    bus.emit("evt", "x")
    assert order == ["first", "second"]


@pytest.mark.asyncio
async def test_disposer_removes_exact_duplicate_registration() -> None:
    order: list[str] = []

    def shared(_payload: object) -> None:
        order.append("shared")

    bus = EventBus()
    bus.on("evt", lambda _p: order.append("first"))
    bus.on("evt", shared)
    bus.on("evt", lambda _p: order.append("middle"))
    later_duplicate = bus.on("evt", shared)
    bus.on("evt", lambda _p: order.append("last"))

    await later_duplicate.dispose()
    bus.emit("evt", None)
    assert order == ["first", "shared", "middle", "last"]


@pytest.mark.asyncio
async def test_waterfall_next_is_single_use() -> None:
    terminal_calls = 0

    async def calls_twice(_payload: object, next) -> None:
        await next()
        await next()

    async def terminal() -> None:
        nonlocal terminal_calls
        terminal_calls += 1

    bus = EventBus()
    bus.on("wf", calls_twice)

    with pytest.raises(RuntimeError, match=r"next\(\) may only be called once"):
        await bus.waterfall("wf", None, next=terminal)
    assert terminal_calls == 1


@pytest.mark.asyncio
async def test_waterfall_concurrent_next_calls_only_dispatch_once() -> None:
    terminal_calls = 0

    async def calls_concurrently(_payload: object, next) -> None:
        await asyncio.gather(next(), next())

    async def terminal() -> None:
        nonlocal terminal_calls
        terminal_calls += 1
        await asyncio.sleep(0)

    bus = EventBus()
    bus.on("wf", calls_concurrently)

    with pytest.raises(RuntimeError, match=r"next\(\) may only be called once"):
        await bus.waterfall("wf", None, next=terminal)
    assert terminal_calls == 1


@pytest.mark.asyncio
async def test_emit_propagates_synchronous_cancelled_error() -> None:
    reached: list[str] = []

    def cancelled(_payload: object) -> None:
        raise asyncio.CancelledError

    bus = EventBus()
    bus.on("evt", cancelled)
    bus.on("evt", lambda _p: reached.append("later"))

    with pytest.raises(asyncio.CancelledError):
        bus.emit("evt", None)
    assert reached == []


@pytest.mark.asyncio
async def test_emit_async_cancelled_error_is_task_local(caplog: pytest.LogCaptureFixture) -> None:
    reached: list[str] = []

    async def cancelled(_payload: object) -> None:
        raise asyncio.CancelledError

    async def sibling(_payload: object) -> None:
        reached.append("sibling")

    bus = EventBus()
    bus.on("evt", cancelled)
    bus.on("evt", sibling)
    bus.emit("evt", None)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert reached == ["sibling"]
    assert "async emit listener" not in caplog.text
