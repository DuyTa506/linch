"""linch.evals — evaluation harness for agent loops.

Provides:
- ``ScriptedProvider`` / ``TextTurn`` / ``ToolUseTurn`` — deterministic provider
- ``EvalCase`` / ``CaseResult`` / ``EvalResult`` — structured results
- ``run_eval`` — run an agent over a list of cases and score outputs
- Built-in scorers for text, tools, schema, cost, context, memory, and recovery
"""

from .harness import CaseResult, EvalCase, EvalResult, run_eval
from .scorers import (
    context_metadata_contains,
    context_not_trimmed,
    context_selected_tool,
    cost_under,
    memory_recalled,
    recovery_succeeded,
    run_completed,
    schema_valid,
    text_contains,
    tool_called,
)
from .scripted import ScriptedProvider, TextTurn, ToolUseTurn

__all__ = [
    # Harness
    "run_eval",
    "EvalCase",
    "CaseResult",
    "EvalResult",
    # Scripted provider
    "ScriptedProvider",
    "TextTurn",
    "ToolUseTurn",
    # Scorers
    "text_contains",
    "tool_called",
    "schema_valid",
    "cost_under",
    "context_selected_tool",
    "context_not_trimmed",
    "context_metadata_contains",
    "memory_recalled",
    "recovery_succeeded",
    "run_completed",
]
