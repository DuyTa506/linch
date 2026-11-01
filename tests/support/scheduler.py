"""Common scheduler harness objects for unit tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any


def scheduler_agent(
    registry: Any,
    *,
    permission_engine: Any = None,
    context: Any = None,
    max_tool_concurrency: int = 8,
    tool_concurrency: int | None = None,
    tool_timeout_ms: float | None = None,
    tool_retry: Any = None,
    hooks: tuple[Any, ...] | list[Any] = (),
    cwd: str = ".",
    **overrides: Any,
) -> SimpleNamespace:
    """Build the inert agent namespace consumed by scheduler unit tests.

    Args:
        registry: Tool registry exposed to the scheduler.
        permission_engine: Optional permission engine; defaults to skip-dangerous.
        context: Optional Linch context for event listeners.
        max_tool_concurrency: Maximum number of admitted calls.
        tool_concurrency: Legacy concurrency alias used by scheduler tests.
        tool_timeout_ms: Optional agent-wide execution timeout.
        tool_retry: Optional retry configuration.
        hooks: Hook objects invoked by the scheduler.
        cwd: Working directory exposed through the agent namespace.
        overrides: Additional attributes required by a focused test.

    Returns:
        A minimal scheduler-compatible agent namespace.
    """
    from linch import Context
    from linch.permissions import PermissionEngine

    values: dict[str, Any] = {
        "cwd": cwd,
        "tools": registry,
        "permission_engine": permission_engine or PermissionEngine(mode="skip-dangerous"),
        "max_tool_concurrency": max_tool_concurrency,
        "tool_concurrency": (
            max_tool_concurrency if tool_concurrency is None else tool_concurrency
        ),
        "tool_timeout_ms": tool_timeout_ms,
        "tool_retry": tool_retry,
        "hooks": list(hooks),
        "result_offload": None,
        "tool_pipeline": None,
        "run_store": None,
        "enable_background_tools": False,
        "context": context or Context(),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def scheduler_session(
    *,
    session_id: str = "s1",
    run_id: str | None = "run-1",
    principal: Any = None,
    **overrides: Any,
) -> SimpleNamespace:
    """Build the inert session namespace consumed by scheduler unit tests.

    Args:
        session_id: Session identifier passed to tool context.
        run_id: Active run identifier, or ``None`` for an idle session.
        principal: Principal bound to the active run.
        overrides: Additional state or callbacks required by a focused test.

    Returns:
        A minimal scheduler-compatible session namespace.
    """
    values: dict[str, Any] = {
        "id": session_id,
        "store": None,
        "active_run_id": run_id,
        "_active_principal": principal,
        "tools_override": None,
        "current_turn_allowed_tools": None,
        "current_turn_permission_decisions": {},
        "run_deps": None,
        "filesystem": None,
        "background_tasks": [],
        "pending_notifications": [],
        "pending_child_events": [],
    }

    async def notify(message: Any, **_: Any) -> None:
        values["pending_notifications"].append(message)

    values["notify"] = notify
    values.update(overrides)
    return SimpleNamespace(**values)
