from __future__ import annotations

import pytest

from linch.hooks import AgentStartContext, HookDispatcher, HookEvent
from linch.kernel import EventBus
from linch.lifecycle import (
    AGENT_START,
    LIFECYCLE_POINTS,
    TOOL_EXECUTE,
    TOOL_PRE_EXECUTE,
    TURN_STOPPING,
)


def test_catalog_points_have_names_payloads_and_modes() -> None:
    assert LIFECYCLE_POINTS
    assert all(point.name and point.payload_type and point.mode for point in LIFECYCLE_POINTS)
    assert TOOL_EXECUTE.payload_type.__name__ == "ToolExecution"
    hook_names = {event.value for event in HookEvent}
    hook_points = [point for point in LIFECYCLE_POINTS if point.name in hook_names]
    assert len(hook_points) == 21
    assert all(point.mode == "hook" for point in hook_points)


@pytest.mark.asyncio
async def test_event_bus_rejects_a_dispatch_mode_mismatch_without_listeners() -> None:
    bus = EventBus()

    with pytest.raises(ValueError, match="tools/execute.*waterfall"):
        await bus.serial(TOOL_EXECUTE, object())

    with pytest.raises(ValueError, match="tools/pre-execute.*waterfall"):
        bus.emit(TOOL_PRE_EXECUTE, object())

    with pytest.raises(ValueError, match="turn-stopping.*serial"):
        bus.emit(TURN_STOPPING, object())


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["emit", "serial", "waterfall"])
async def test_event_bus_rejects_hook_points_for_every_kernel_dispatcher(method: str) -> None:
    bus = EventBus()
    dispatch = getattr(bus, method)
    with pytest.raises(ValueError, match="AgentStart.*hook"):
        if method == "emit":
            dispatch(AGENT_START, object())
        elif method == "serial":
            await dispatch(AGENT_START, object())
        else:
            await dispatch(AGENT_START, object(), next=lambda: None)


@pytest.mark.asyncio
async def test_event_bus_accepts_typed_serial_and_waterfall_points() -> None:
    bus = EventBus()
    seen: list[object] = []
    bus.on(TURN_STOPPING, lambda payload: seen.append(payload))
    assert await bus.serial(TURN_STOPPING, "x") is None
    assert seen == ["x"]


@pytest.mark.asyncio
async def test_hook_dispatcher_accepts_catalog_points_and_rejects_other_modes() -> None:
    dispatcher = HookDispatcher()
    context = AgentStartContext(session=None, run_id="r", turn_index=None)
    assert (await dispatcher.dispatch(AGENT_START, context)).result.action == "continue"
    with pytest.raises(ValueError, match="waterfall.*HookDispatcher"):
        await dispatcher.dispatch(TOOL_EXECUTE, context)
