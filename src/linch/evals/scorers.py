"""Built-in scorers for the evals harness.

Each scorer is a factory that returns a callable. The callable is invoked
with keyword arguments drawn from the eval run context:
  - output: str          — final text from the model
  - events: list[Event]  — all events from the run
  - result_event         — the ResultEvent (or None)

Return True (pass), False (fail), or None (unknown/skip).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any


def text_contains(substring: str) -> Callable[..., bool | None]:
    """Pass when *substring* appears in the output (case-insensitive).

    Supports ``{expected}`` interpolation when the scorer is applied per-case
    by ``run_eval`` (the harness substitutes the case's expected value).
    """

    def _score(output: str = "", **_: Any) -> bool:
        return substring.lower() in output.lower()

    _score.__name__ = f"text_contains:{substring}"
    _score._template = substring  # type: ignore[attr-defined]
    return _score


def tool_called(tool_name: str) -> Callable[..., bool | None]:
    """Pass when a ``ToolCallStartEvent`` with *tool_name* appears in events."""
    from ..events import ToolCallStartEvent

    def _score(events: list | None = None, **_: Any) -> bool:
        return any(
            isinstance(e, ToolCallStartEvent) and e.tool_name == tool_name for e in (events or [])
        )

    _score.__name__ = f"tool_called:{tool_name}"
    return _score


def schema_valid(schema: dict[str, Any]) -> Callable[..., bool | None]:
    """Pass when the output is valid JSON that satisfies *schema*.

    Falls back gracefully when ``jsonschema`` is not installed — returns None.
    """

    def _score(output: str = "", **_: Any) -> bool | None:
        try:
            data = json.loads(output)
        except (json.JSONDecodeError, ValueError):
            return False
        try:
            import jsonschema  # type: ignore[import]

            jsonschema.validate(data, schema)
            return True
        except jsonschema.ValidationError:
            return False
        except ImportError:
            return None

    _score.__name__ = "schema_valid"
    return _score


def cost_under(budget_usd: float) -> Callable[..., bool | None]:
    """Pass when ``ResultEvent.total_cost_usd`` is below *budget_usd*.

    Returns None when the cost is unknown (total_cost_usd is None).
    """

    def _score(result_event: Any = None, **_: Any) -> bool | None:
        if result_event is None:
            return None
        cost = getattr(result_event, "total_cost_usd", None)
        if cost is None:
            return None
        return cost < budget_usd

    _score.__name__ = f"cost_under:{budget_usd}"
    return _score


def context_selected_tool(tool_name: str) -> Callable[..., bool | None]:
    """Pass when any context build selected *tool_name* for the provider turn."""
    from ..events import ContextBuildEvent

    def _score(events: list | None = None, **_: Any) -> bool | None:
        builds = [e for e in (events or []) if isinstance(e, ContextBuildEvent)]
        if not builds:
            return None
        return any(
            isinstance(event.selected_tools, list) and tool_name in event.selected_tools
            for event in builds
        )

    _score.__name__ = f"context_selected_tool:{tool_name}"
    return _score


def context_not_trimmed() -> Callable[..., bool | None]:
    """Pass when context builds exist and none report ``budget.trimmed=True``."""
    from ..events import ContextBuildEvent

    def _score(events: list | None = None, **_: Any) -> bool | None:
        builds = [e for e in (events or []) if isinstance(e, ContextBuildEvent)]
        if not builds:
            return None
        return all(event.budget.get("trimmed") is not True for event in builds)

    _score.__name__ = "context_not_trimmed"
    return _score


def context_metadata_contains(key: str, expected: Any = None) -> Callable[..., bool | None]:
    """Pass when any context build metadata contains *key*.

    If *expected* is not ``None``, the metadata value must equal it.
    """
    from ..events import ContextBuildEvent

    def _score(events: list | None = None, **_: Any) -> bool | None:
        builds = [e for e in (events or []) if isinstance(e, ContextBuildEvent)]
        if not builds:
            return None
        for event in builds:
            if key not in event.metadata:
                continue
            if expected is None or event.metadata.get(key) == expected:
                return True
        return False

    _score.__name__ = f"context_metadata_contains:{key}"
    return _score


def memory_recalled(expected_ids: str | list[str] | tuple[str, ...]) -> Callable[..., bool | None]:
    """Pass when SearchMemory returns all expected memory ids."""
    from ..events import ToolCallEndEvent

    required = {expected_ids} if isinstance(expected_ids, str) else set(expected_ids)

    def _score(events: list | None = None, **_: Any) -> bool | None:
        memory_calls = [
            e
            for e in (events or [])
            if isinstance(e, ToolCallEndEvent) and e.tool_name == "SearchMemory"
        ]
        if not memory_calls:
            return None

        found: set[str] = set()
        for event in memory_calls:
            if event.tool_result is None:
                continue
            _add_strings(found, event.tool_result.metadata.get("result_ids"))
            for citation in event.tool_result.citations:
                found.add(citation.id)
        return required.issubset(found)

    _score.__name__ = "memory_recalled:" + ",".join(sorted(required))
    return _score


def recovery_succeeded(tool_name: str | None = None) -> Callable[..., bool | None]:
    """Pass when a failed tool call is followed by a successful tool call.

    When *tool_name* is provided, both calls must be for that tool.
    """
    from ..events import ToolCallEndEvent

    def _score(events: list | None = None, **_: Any) -> bool | None:
        saw_failure = False
        saw_tool = False
        for event in events or []:
            if not isinstance(event, ToolCallEndEvent):
                continue
            if tool_name is not None and event.tool_name != tool_name:
                continue
            saw_tool = True
            if event.is_error:
                saw_failure = True
            elif saw_failure:
                return True
        return False if saw_tool else None

    _score.__name__ = f"recovery_succeeded:{tool_name or '*'}"
    return _score


def run_completed() -> Callable[..., bool | None]:
    """Pass when the run reaches a successful ``ResultEvent``."""

    def _score(result_event: Any = None, **_: Any) -> bool | None:
        if result_event is None:
            return None
        return getattr(result_event, "subtype", None) == "success"

    _score.__name__ = "run_completed"
    return _score


def _add_strings(target: set[str], value: Any) -> None:
    if isinstance(value, str):
        target.add(value)
    elif isinstance(value, list | tuple):
        for item in value:
            if isinstance(item, str):
                target.add(item)
