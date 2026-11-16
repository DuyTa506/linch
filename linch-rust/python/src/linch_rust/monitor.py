"""Monitoring: evals and run reports, as typed dicts and the call helpers.

`Agent.eval` runs cases on an open agent and scores them; `Agent.report`
reads the report of one stored run. Built-in scorers run in the server;
a custom scorer is a Python callable the client applies to each case's
result, after the run.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, NotRequired, Required, TypedDict

from ._rpc import Connection


class EvalCase(TypedDict, total=False):
    """One eval case: a prompt for a fresh session and the answer to expect.

    Attributes:
        prompt: What the agent runs on; not empty.
        expected: The expected answer; `text_contains` puts it in place of
            `{expected}`. Empty when absent.
        metadata: Free-form data about the case, returned with its result.
    """

    prompt: Required[str]
    expected: str
    metadata: Mapping[str, Any]


class Scorer(TypedDict, total=False):
    """A built-in scorer: its `type` and that type's own fields.

    The types are `text_contains` (`substring`, `{expected}` when absent),
    `tool_called` (`tool`), `schema_valid` (`schema`), `cost_under`
    (`budget_usd`), `context_selected_tool` (`tool`), `context_not_trimmed`,
    `context_metadata_contains` (`key`, optionally `expected`),
    `recovery_succeeded` (optionally `tool`) and `run_completed`. An unknown
    type or field, or an empty string where one is required, is a
    `ProtocolError`.
    """

    type: Required[str]
    substring: str
    tool: str
    schema: Mapping[str, Any]
    budget_usd: float
    key: str
    expected: Any


class Usage(TypedDict):
    """Token counts of a run or a sum of runs."""

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int


class Score(TypedDict):
    """One scorer's verdict: `True` passes, `False` fails, `None` is unknown.

    A scorer is unknown when the run lacks the data it reads, such as
    `cost_under` on a model without a price.
    """

    name: str
    verdict: bool | None


class CaseResult(TypedDict):
    """The outcome of one case.

    Attributes:
        case: The case that ran.
        output: The run's final text; empty when it produced none.
        passed: Whether the run recorded no error and no scorer failed.
        scores: One verdict per scorer, in order.
        error: The last error the run reported, if any.
        run_id: The stored run, for `Agent.report` and `Agent.events`.
        event_count: How many events the run streamed.
        tool_calls: Names of the tools the run started, in call order.
        total_cost_usd: What the run cost; `None` for a model without a
            price (see `AgentConfig.pricing`).
        usage: The run's token usage; `None` for a run with no result.
        duration_ms: How long the run took; `None` for a run with no
            result.
        events: Every event of the run, as the wire envelopes; only with
            `include_events`.
    """

    case: EvalCase
    output: str
    passed: bool
    scores: list[Score]
    error: str | None
    run_id: str | None
    event_count: int
    tool_calls: list[str]
    total_cost_usd: float | None
    usage: Usage | None
    duration_ms: int | None
    events: NotRequired[list[dict[str, Any]]]


class EvalReport(TypedDict):
    """The result of `Agent.eval`.

    Attributes:
        total: Number of cases.
        passed: Number of cases that passed.
        pass_rate: `passed` over `total`; 0 without cases.
        total_cost_usd: The sum of the cases' costs; `None` when no case
            has one.
        usage: The sum of the cases' token usage.
        duration_ms: The sum of the cases' run durations.
        cases: One result per case, in order.
        markdown: The Markdown report; absent when a custom scorer ran,
            since the server did not see its verdicts.
    """

    total: int
    passed: int
    pass_rate: float
    total_cost_usd: float | None
    usage: Usage
    duration_ms: int
    cases: list[CaseResult]
    markdown: NotRequired[str]


class ToolCallRecord(TypedDict):
    """One tool call of a run: its input, result and how long it took."""

    tool_use_id: str
    tool_name: str
    input: dict[str, Any]
    summary: str
    result: str
    is_error: bool
    duration_ms: int


class RunReport(TypedDict):
    """One run folded into a report.

    Attributes:
        run_id: The run.
        session_id: Its session.
        status: `running`, `awaiting_approval`, `awaiting_input`,
            `completed`, `error`, `aborted` or `interrupted`.
        phase: The checkpoint phase's wire tag.
        event_count: Number of events read.
        tool_calls: Every ended tool call, in end order, with its duration.
        permission_requests: Every tool call that asked for permission.
        context_builds: Every `context_build` event.
        loop_guards: Every `loop_guard` event.
        errors: Every `error` event's payload.
        usage: The last `usage` event.
        final: The `result` event, with the total cost, usage and duration.
        checkpoint: The run record's checkpoint.
        summary: Event counts, token usage and cache ratio, tool timings
            (total, average, maximum, the slowest and the failing ones),
            context, recovery, prompt cache and risk figures.
        long_run: Signals for long runs: context growth, quality, resume.
        timeline: Every event; empty unless asked for with `timeline=True`.
    """

    run_id: str
    session_id: str
    status: str
    phase: str | None
    event_count: int
    tool_calls: list[ToolCallRecord]
    permission_requests: list[dict[str, Any]]
    context_builds: list[dict[str, Any]]
    loop_guards: list[dict[str, Any]]
    errors: list[dict[str, Any]]
    usage: dict[str, Any] | None
    final: dict[str, Any] | None
    checkpoint: dict[str, Any] | None
    summary: dict[str, Any]
    long_run: dict[str, Any]
    timeline: list[dict[str, Any]]


class RunReportResult(TypedDict):
    """The answer of `Agent.report`: the report and its Markdown form."""

    report: RunReport
    markdown: str


CustomScorer = Callable[[CaseResult], "bool | None | Awaitable[bool | None]"]
"""A scorer in Python: given a case's result, `True`, `False`, or `None`
for unknown. It may be async. Its name in the report is its `__name__`."""


def _split(scorers: Sequence[Scorer | CustomScorer]) -> tuple[list[Scorer], list[CustomScorer]]:
    built_in: list[Scorer] = []
    custom: list[CustomScorer] = []
    for scorer in scorers:
        if callable(scorer):
            custom.append(scorer)
        else:
            built_in.append(scorer)
    return built_in, custom


async def evaluate(
    conn: Connection,
    agent: str,
    cases: Sequence[EvalCase],
    scorers: Sequence[Scorer | CustomScorer],
    include_events: bool,
) -> EvalReport:
    """Call `eval.run`, then apply the custom scorers to each case result."""
    built_in, custom = _split(scorers)
    params: dict[str, Any] = {
        "agent": agent,
        "cases": [dict(case) for case in cases],
        "scorers": [dict(scorer) for scorer in built_in],
    }
    if include_events:
        params["include_events"] = True
    answer = await conn.request("eval.run", params)
    report: EvalReport = answer  # type: ignore[assignment]
    if custom:
        await _apply(report, custom)
    return report


async def _apply(report: EvalReport, custom: Sequence[CustomScorer]) -> None:
    """Add the verdicts of `custom` to each case and recompute the pass counts."""
    for case in report["cases"]:
        for scorer in custom:
            verdict: Any = scorer(case)
            if inspect.isawaitable(verdict):
                verdict = await verdict
            if verdict is not None and not isinstance(verdict, bool):
                name = getattr(scorer, "__name__", "custom")
                raise TypeError(f"scorer {name!r} returned {verdict!r}, not True, False or None")
            case["scores"].append(
                {"name": getattr(scorer, "__name__", "custom"), "verdict": verdict}
            )
        failed = any(score["verdict"] is False for score in case["scores"])
        case["passed"] = case["error"] is None and not failed
    passed = sum(1 for case in report["cases"] if case["passed"])
    report["passed"] = passed
    report["pass_rate"] = passed / report["total"] if report["total"] else 0.0
    report.pop("markdown", None)


async def run_report(conn: Connection, agent: str, run_id: str, timeline: bool) -> RunReportResult:
    """Call `run.report`."""
    params: dict[str, Any] = {"agent": agent, "run_id": run_id}
    if timeline:
        params["timeline"] = True
    answer = await conn.request("run.report", params)
    return answer  # type: ignore[return-value]
