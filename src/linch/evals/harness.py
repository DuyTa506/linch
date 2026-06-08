"""run_eval — thin evaluation harness for the Linch agent loop."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class EvalCase:
    """A single evaluation case."""

    prompt: str
    expected: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class CaseResult:
    """Result for a single EvalCase."""

    case: EvalCase
    output: str
    passed: bool
    scores: dict[str, bool | None]
    events: list = field(default_factory=list)
    error: str | None = None


@dataclass
class EvalResult:
    """Aggregated result over all eval cases."""

    cases: list[CaseResult]

    @property
    def total(self) -> int:
        return len(self.cases)

    @property
    def passed(self) -> int:
        return sum(1 for c in self.cases if c.passed)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0


async def run_eval(
    agent: Any,
    cases: list[EvalCase],
    scorers: list[Callable[..., bool | None]] | None = None,
) -> EvalResult:
    """Run *agent* over each case and score the outputs.

    For each case:
    1. A fresh session is created.
    2. The agent runs with ``case.prompt``.
    3. Each scorer is called with ``output``, ``events``, and ``result_event`` kwargs.
       Scorers that use ``{expected}`` in their template have it substituted first.
    4. A case passes when every scorer returns True (or None for unknown).

    Args:
        agent:   A configured ``Agent`` instance.
        cases:   List of ``EvalCase`` objects.
        scorers: Optional list of scorer callables from ``linch.evals``.
                 If empty/None, cases pass if the run completes without error.

    Returns:
        ``EvalResult`` with aggregated pass/fail counts and per-case details.
    """
    from ..events import ResultEvent

    scorers = scorers or []
    results: list[CaseResult] = []

    for case in cases:
        session = await agent.session()
        events: list = []
        result_event: ResultEvent | None = None
        output = ""
        error: str | None = None

        try:
            async for event in session.run(case.prompt):
                events.append(event)
                if event.type == "result":
                    result_event = event
                    output = event.final_text or ""
                elif event.type == "error":
                    error = str(event.error)
        except Exception as exc:
            error = str(exc)

        scores: dict[str, bool | None] = {}
        for scorer in scorers:
            name = getattr(scorer, "__name__", repr(scorer))
            # Substitute {expected} in text_contains scorers
            actual_scorer = scorer
            template = getattr(scorer, "_template", None)
            if template is not None and "{expected}" in template:
                from .scorers import text_contains

                actual_scorer = text_contains(template.replace("{expected}", case.expected))
            try:
                verdict = actual_scorer(
                    output=output,
                    events=events,
                    result_event=result_event,
                )
            except Exception:
                verdict = None
            scores[name] = verdict

        # A case passes when there are no errors and every non-None score is True.
        non_none = [v for v in scores.values() if v is not None]
        passed = error is None and (not non_none or all(non_none))

        results.append(
            CaseResult(
                case=case,
                output=output,
                passed=passed,
                scores=scores,
                events=events,
                error=error,
            )
        )

    return EvalResult(cases=results)
