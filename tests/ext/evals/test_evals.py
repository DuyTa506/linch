"""Tests for Feature D — linch.ext.evals harness (RED until implemented).

Tests cover:
- ScriptedProvider: canonical scripted fake provider
- EvalCase / CaseResult / EvalResult dataclasses
- Built-in scorers: text_contains, tool_called, schema_valid, cost_under
- run_eval end-to-end
"""

from __future__ import annotations

import builtins

import pytest

from tests.support.agents import make_agent


def _eval_agent(provider, *, tools=None, run_store=None):
    """Build the inert agent shared by end-to-end evaluator cases."""
    return make_agent(provider, model="fake-model", tools=tools, run_store=run_store)


# ---------------------------------------------------------------------------
# Unit: ScriptedProvider
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scripted_provider_text_response():
    """ScriptedProvider with a text turn returns the configured text."""
    from linch.ext.evals import ScriptedProvider, TextTurn
    from linch.types import ProviderRequest

    provider = ScriptedProvider(turns=[TextTurn(text="Paris is the capital.")])
    req = ProviderRequest(model="fake-model", system=[], tools=[], messages=[])

    events = [event async for event in provider.stream(req)]

    assert any(
        event["type"] == "text_delta" and event["text"] == "Paris is the capital."
        for event in events
    )


@pytest.mark.asyncio
async def test_scripted_provider_tool_use_turn():
    """ScriptedProvider supports ToolUseTurn (tool call + final text)."""
    from linch.ext.evals import ScriptedProvider, ToolUseTurn
    from linch.types import ProviderRequest

    provider = ScriptedProvider(
        turns=[
            ToolUseTurn(tool_name="Read", tool_input={"file_path": "README.md"}),
        ]
    )
    req = ProviderRequest(model="fake-model", system=[], tools=[], messages=[])

    events = [event async for event in provider.stream(req)]

    assert any(event["type"] == "tool_use_start" and event["name"] == "Read" for event in events)
    assert any(
        event["type"] == "tool_use_input_delta" and "README.md" in event["json_delta"]
        for event in events
    )


# ---------------------------------------------------------------------------
# Unit: EvalCase / EvalResult / CaseResult dataclasses
# ---------------------------------------------------------------------------


def test_eval_case_dataclass():
    """EvalCase holds prompt + expected + optional metadata."""
    from linch.ext.evals import EvalCase

    case = EvalCase(prompt="What is 2+2?", expected="4")
    assert case.prompt == "What is 2+2?"
    assert case.expected == "4"


def test_case_result_dataclass():
    """CaseResult holds case + output + scorer verdicts."""
    from linch.ext.evals import CaseResult, EvalCase

    case = EvalCase(prompt="hi", expected="hello")
    result = CaseResult(case=case, output="hello world", passed=True, scores={})
    assert result.passed is True
    assert result.output == "hello world"


def test_eval_result_dataclass():
    """EvalResult aggregates CaseResults with pass_rate."""
    from linch.ext.evals import CaseResult, EvalCase, EvalResult

    cases = [
        CaseResult(case=EvalCase(prompt="a", expected="x"), output="x", passed=True, scores={}),
        CaseResult(case=EvalCase(prompt="b", expected="y"), output="nope", passed=False, scores={}),
    ]
    result = EvalResult(cases=cases)
    assert result.pass_rate == 0.5
    assert result.passed == 1
    assert result.total == 2


def test_eval_result_report_exports():
    """EvalResult exports dict and markdown reports with scores, tools, and cost."""
    from linch.events import ResultEvent, ToolCallStartEvent
    from linch.ext.evals import CaseResult, EvalCase, EvalResult
    from linch.types import Usage

    case_result = CaseResult(
        case=EvalCase(prompt="capital?", expected="Paris", metadata={"kind": "geo"}),
        output="Paris",
        passed=True,
        scores={"text_contains": True, "cost_under": True},
        events=[
            ToolCallStartEvent(
                tool_use_id="t1",
                tool_name="SearchDocs",
                input={"query": "Paris"},
                summary="SearchDocs",
            ),
            ResultEvent(
                subtype="success",
                stop_reason="end_turn",
                total_usage=Usage(input_tokens=3, output_tokens=2),
                duration_ms=10,
                final_text="Paris",
                total_cost_usd=0.01,
            ),
        ],
    )
    result = EvalResult(cases=[case_result])

    exported = result.to_dict(include_events=True)
    markdown = result.to_markdown()

    assert exported["total"] == 1
    assert exported["cases"][0]["case"]["metadata"] == {"kind": "geo"}
    assert exported["cases"][0]["tool_calls"] == ["SearchDocs"]
    assert exported["cases"][0]["total_cost_usd"] == 0.01
    assert exported["cases"][0]["events"][0]["type"] == "tool_call_start"
    assert "Linch Eval Report" in markdown
    assert "SearchDocs" in markdown


# ---------------------------------------------------------------------------
# Unit: Built-in scorers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("needle", "output", "expected"),
    [
        ("Paris", "The capital is Paris.", True),
        ("Paris", "The capital is London.", False),
        ("paris", "The capital is PARIS.", True),
    ],
)
def test_text_contains_scorer_pass(needle, output, expected):
    """text_contains handles matches, misses, and case-insensitive output."""
    from linch.ext.evals import text_contains

    assert text_contains(needle)(output) is expected


@pytest.mark.parametrize("events_present", [True, False])
def test_tool_called_scorer_pass(events_present):
    """tool_called reports whether the requested tool appears in events."""
    from linch.events import ToolCallStartEvent
    from linch.ext.evals import tool_called

    events = (
        [ToolCallStartEvent(tool_use_id="t1", tool_name="Read", input={}, summary="Read()")]
        if events_present
        else []
    )
    assert tool_called("Read")(events=events) is events_present


@pytest.mark.parametrize(
    ("schema", "output", "expected"),
    [
        (
            {
                "type": "object",
                "properties": {"capital": {"type": "string"}},
                "required": ["capital"],
            },
            '{"capital": "Paris"}',
            True,
        ),
        (
            {
                "type": "object",
                "properties": {"capital": {"type": "string"}},
                "required": ["capital"],
            },
            '{"city": "Paris"}',
            False,
        ),
        ({"type": "object"}, "not json at all", False),
    ],
)
def test_schema_valid_scorer_pass(schema, output, expected):
    """schema_valid handles valid, incomplete, and malformed JSON output."""
    from linch.ext.evals import schema_valid

    assert schema_valid(schema)(output) is expected


def test_schema_valid_scorer_fails_closed_without_jsonschema(monkeypatch):
    from linch.ext.evals import schema_valid

    real_import = builtins.__import__

    def import_without_jsonschema(name, *args, **kwargs):
        if name == "jsonschema":
            raise ImportError("jsonschema intentionally unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_jsonschema)

    scorer = schema_valid({"type": "object"})
    assert scorer('{"capital": "Paris"}') is False


def test_schema_valid_scorer_rejects_invalid_schema():
    from linch.ext.evals import schema_valid

    scorer = schema_valid({"type": "not-a-json-schema-type"})
    assert scorer('{"capital": "Paris"}') is False


@pytest.mark.parametrize(
    ("limit", "cost", "expected"),
    [(0.10, 0.01, True), (0.01, 0.05, False), (0.10, None, None)],
)
def test_cost_under_scorer_pass(limit, cost, expected):
    """cost_under reports under-budget, over-budget, and unknown costs."""
    from linch.events import ResultEvent
    from linch.ext.evals import cost_under
    from linch.types import Usage

    result_event = ResultEvent(
        subtype="success",
        stop_reason="end_turn",
        total_usage=Usage(),
        duration_ms=100,
        total_cost_usd=cost,
    )
    assert cost_under(limit)(result_event=result_event) is expected


def test_long_run_context_scorers():
    from linch.events import ContextBuildEvent
    from linch.ext.evals import (
        context_metadata_contains,
        context_not_trimmed,
        context_selected_tool,
    )

    events = [
        ContextBuildEvent(
            system_blocks=1,
            messages=2,
            selected_tools=["SearchDocs", "read_file"],
            budget={"trimmed": False},
            metadata={"source": "docs"},
        )
    ]

    assert context_selected_tool("SearchDocs")(events=events) is True
    assert context_selected_tool("read_file")(events=events) is True
    assert context_selected_tool("Bash")(events=events) is False
    assert context_not_trimmed()(events=events) is True
    assert context_metadata_contains("source", "docs")(events=events) is True


def test_recovery_and_completion_scorers():
    from linch.events import ResultEvent, ToolCallEndEvent
    from linch.ext.evals import recovery_succeeded, run_completed
    from linch.types import Usage

    events = [
        ToolCallEndEvent(tool_use_id="t1", tool_name="Read", is_error=True),
        ToolCallEndEvent(tool_use_id="t2", tool_name="Read", is_error=False),
    ]
    result_event = ResultEvent(
        subtype="success",
        stop_reason="end_turn",
        total_usage=Usage(),
        duration_ms=10,
    )

    assert recovery_succeeded("Read")(events=events) is True
    assert recovery_succeeded("Bash")(events=events) is None
    assert run_completed()(result_event=result_event) is True


def test_scorer_factories_have_distinct_names():
    from linch.ext.evals import context_selected_tool, cost_under, text_contains

    names = {
        text_contains("Paris").__name__,
        cost_under(0.05).__name__,
        context_selected_tool("SearchDocs").__name__,
    }

    assert names == {
        "text_contains:Paris",
        "cost_under:0.05",
        "context_selected_tool:SearchDocs",
    }


# ---------------------------------------------------------------------------
# End-to-end: run_eval
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("texts", "case_specs", "total", "passed", "pass_rate"),
    [
        (
            ["The capital is Paris."],
            [("What is the capital of France?", "Paris")],
            1,
            1,
            1.0,
        ),
        (
            ["Paris", "Paris"],
            [("Capital of France?", "Paris"), ("Capital of Germany?", "Berlin")],
            2,
            1,
            0.5,
        ),
    ],
)
async def test_run_eval_cases_aggregate(texts, case_specs, total, passed, pass_rate):
    """run_eval handles one and multiple cases with the expected aggregate."""
    from linch.ext.evals import EvalCase, ScriptedProvider, TextTurn, run_eval, text_contains

    provider = ScriptedProvider(turns=[TextTurn(text=text) for text in texts])
    agent = _eval_agent(provider)

    cases = [EvalCase(prompt=prompt, expected=expected) for prompt, expected in case_specs]
    result = await run_eval(agent, cases, scorers=[text_contains("{expected}")])

    assert result.total == total
    assert result.passed == passed
    assert result.pass_rate == pass_rate


@pytest.mark.asyncio
async def test_run_eval_tool_called_scorer():
    """run_eval with tool_called scorer detects tool invocation."""
    from linch.ext.evals import (
        EvalCase,
        ScriptedProvider,
        TextTurn,
        ToolUseTurn,
        run_eval,
        tool_called,
    )
    from tests.support.tools import StubTool, tool_registry

    reg = tool_registry(StubTool("Echo", output="hello", parallel=True))

    provider = ScriptedProvider(
        turns=[
            ToolUseTurn(tool_name="Echo", tool_input={"text": "hello"}),
            TextTurn(text="done"),
        ]
    )
    agent = _eval_agent(provider, tools=reg)

    cases = [EvalCase(prompt="Echo hello", expected="Echo")]
    result = await run_eval(agent, cases, scorers=[tool_called("Echo")])

    assert result.passed == 1


@pytest.mark.asyncio
async def test_run_eval_colliding_scorer_names_not_lost():
    """Two scorers sharing a __name__ must both be retained (Bug A).

    The first scorer fails and the second passes; because both hardcode the same
    ``__name__``, a naive ``scores[name] = verdict`` would drop the failing
    verdict and report the case as passing. The case must be reported FAILED.
    """
    from linch.ext.evals import EvalCase, ScriptedProvider, TextTurn, run_eval, schema_valid

    provider = ScriptedProvider(turns=[TextTurn(text='{"capital": "Paris"}')])
    agent = _eval_agent(provider)

    # Both factories hardcode __name__ == "schema_valid".
    failing = schema_valid(
        {"type": "object", "properties": {"capital": {"type": "number"}}, "required": ["capital"]}
    )
    passing = schema_valid(
        {"type": "object", "properties": {"capital": {"type": "string"}}, "required": ["capital"]}
    )
    assert failing.__name__ == passing.__name__ == "schema_valid"

    cases = [EvalCase(prompt="Capital of France as JSON?", expected="Paris")]
    result = await run_eval(agent, cases, scorers=[failing, passing])

    # The failing verdict must not be overwritten/lost.
    assert result.passed == 0
    case_scores = result.cases[0].scores
    assert False in case_scores.values()
    assert True in case_scores.values()


@pytest.mark.asyncio
async def test_run_eval_releases_sessions_per_case():
    """run_eval must not accumulate one session per case in agent._sessions (Bug B)."""
    from linch.ext.evals import EvalCase, ScriptedProvider, TextTurn, run_eval

    provider = ScriptedProvider(
        turns=[TextTurn(text="ok"), TextTurn(text="ok"), TextTurn(text="ok")]
    )
    agent = _eval_agent(provider)

    baseline = len(agent._sessions)
    cases = [
        EvalCase(prompt="a", expected="ok"),
        EvalCase(prompt="b", expected="ok"),
        EvalCase(prompt="c", expected="ok"),
    ]
    await run_eval(agent, cases, scorers=[])

    assert len(agent._sessions) == baseline
