"""linch.evals — evaluation harness for agent loops.

Provides:
- ``ScriptedProvider`` / ``TextTurn`` / ``ToolUseTurn`` — deterministic provider
- ``EvalCase`` / ``CaseResult`` / ``EvalResult`` — structured results
- ``run_eval`` — run an agent over a list of cases and score outputs
- Built-in scorers: ``text_contains``, ``tool_called``, ``schema_valid``, ``cost_under``
"""

from .harness import CaseResult, EvalCase, EvalResult, run_eval
from .scorers import cost_under, schema_valid, text_contains, tool_called
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
]
