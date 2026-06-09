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

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt": self.prompt,
            "expected": self.expected,
            "metadata": dict(self.metadata),
        }


@dataclass
class CaseResult:
    """Result for a single EvalCase."""

    case: EvalCase
    output: str
    passed: bool
    scores: dict[str, bool | None]
    events: list = field(default_factory=list)
    error: str | None = None

    def to_dict(self, *, include_events: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "case": self.case.to_dict(),
            "output": self.output,
            "passed": self.passed,
            "scores": dict(self.scores),
            "error": self.error,
            "event_count": len(self.events),
            "tool_calls": _tool_calls(self.events),
            "total_cost_usd": _total_cost_usd(self.events),
        }
        if include_events:
            from ..events import event_to_dict

            out["events"] = [event_to_dict(event) for event in self.events]
        return out


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

    def to_dict(self, *, include_events: bool = False) -> dict[str, Any]:
        return {
            "total": self.total,
            "passed": self.passed,
            "pass_rate": self.pass_rate,
            "cases": [case.to_dict(include_events=include_events) for case in self.cases],
        }

    def to_markdown(self) -> str:
        lines = [
            "# Linch Eval Report",
            "",
            f"- total: {self.total}",
            f"- passed: {self.passed}",
            f"- pass_rate: {self.pass_rate:.2%}",
            "",
            "| # | Passed | Scores | Cost USD | Tool Calls | Output | Error |",
            "|---:|---|---|---:|---|---|---|",
        ]
        for idx, case in enumerate(self.cases, start=1):
            scores = ", ".join(f"{name}={value}" for name, value in case.scores.items())
            tools = ", ".join(_tool_calls(case.events))
            cost = _total_cost_usd(case.events)
            output = _table_text(case.output)
            error = _table_text(case.error or "")
            lines.append(
                f"| {idx} | {case.passed} | {scores} | {cost if cost is not None else ''} | "
                f"{tools} | {output} | {error} |"
            )
        return "\n".join(lines)


def _tool_calls(events: list) -> list[str]:
    return [
        event.tool_name
        for event in events
        if getattr(event, "type", None) == "tool_call_start"
        and isinstance(getattr(event, "tool_name", None), str)
    ]


def _total_cost_usd(events: list) -> float | None:
    for event in reversed(events):
        if getattr(event, "type", None) == "result":
            value = getattr(event, "total_cost_usd", None)
            return float(value) if isinstance(value, int | float) else None
    return None


def _table_text(value: str) -> str:
    text = " ".join(value.split())
    if len(text) > 120:
        text = text[:117] + "..."
    return text.replace("|", "\\|")


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
        finally:
            # Release this case's session so _sessions (and any spawned
            # background workers) don't accumulate across the suite. There is no
            # per-session close; pop from the registry and abort to cancel any
            # background worker tasks. Never let teardown mask a case error.
            agent._sessions.pop(session.id, None)
            session.abort()

        scores: dict[str, bool | None] = {}
        for scorer in scorers:
            name = getattr(scorer, "__name__", repr(scorer))
            # Disambiguate name collisions so every scorer's verdict is retained.
            # Several scorer factories hardcode a constant __name__ (e.g.
            # "schema_valid"); two of the same would otherwise overwrite each
            # other and drop a failing verdict.
            if name in scores:
                suffix = 2
                while f"{name}#{suffix}" in scores:
                    suffix += 1
                name = f"{name}#{suffix}"
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
