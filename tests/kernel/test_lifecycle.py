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


@pytest.mark.parametrize(
    ("method", "point", "match"),
    [
        ("serial", TOOL_EXECUTE, "tools/execute.*waterfall"),
        ("emit", TOOL_PRE_EXECUTE, "tools/pre-execute.*waterfall"),
        ("emit", TURN_STOPPING, "turn-stopping.*serial"),
        ("emit", AGENT_START, "AgentStart.*hook"),
        ("serial", AGENT_START, "AgentStart.*hook"),
        ("waterfall", AGENT_START, "AgentStart.*hook"),
    ],
)
async def test_event_bus_rejects_invalid_dispatch(method: str, point: object, match: str) -> None:
    bus = EventBus()
    dispatch = getattr(bus, method)
    with pytest.raises(ValueError, match=match):
        if method == "emit":
            dispatch(point, object())
        elif method == "serial":
            await dispatch(point, object())
        else:
            await dispatch(point, object(), next=lambda: None)


async def test_event_bus_accepts_typed_serial_and_waterfall_points() -> None:
    bus = EventBus()
    seen: list[object] = []
    bus.on(TURN_STOPPING, lambda payload: seen.append(payload))
    assert await bus.serial(TURN_STOPPING, "x") is None
    assert seen == ["x"]


async def test_hook_dispatcher_accepts_catalog_points_and_rejects_other_modes() -> None:
    dispatcher = HookDispatcher()
    context = AgentStartContext(session=None, run_id="r", turn_index=None)
    assert (await dispatcher.dispatch(AGENT_START, context)).result.action == "continue"
    with pytest.raises(ValueError, match="waterfall.*HookDispatcher"):
        await dispatcher.dispatch(TOOL_EXECUTE, context)
