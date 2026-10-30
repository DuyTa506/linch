"""Linch 3.0 removes the process-local scheduling API."""

from __future__ import annotations

import inspect


def test_scheduling_names_are_not_public() -> None:
    import linch

    assert not hasattr(linch, "Schedule")
    assert not hasattr(linch, "SchedulerLoop")
    assert not hasattr(linch, "InMemoryScheduleStore")
    assert "Schedule" not in linch.__all__


def test_agent_does_not_accept_schedule_store() -> None:
    from linch import Agent

    assert "schedule_store" not in inspect.signature(Agent).parameters
