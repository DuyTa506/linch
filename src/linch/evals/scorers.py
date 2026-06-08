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

    _score._template = substring  # type: ignore[attr-defined]
    return _score


def tool_called(tool_name: str) -> Callable[..., bool | None]:
    """Pass when a ``ToolCallStartEvent`` with *tool_name* appears in events."""
    from ..events import ToolCallStartEvent

    def _score(events: list | None = None, **_: Any) -> bool:
        return any(
            isinstance(e, ToolCallStartEvent) and e.tool_name == tool_name for e in (events or [])
        )

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

    return _score
