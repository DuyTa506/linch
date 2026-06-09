"""Tests for Feature D — linch.evals harness (RED until implemented).

Tests cover:
- ScriptedProvider: canonical scripted fake provider
- EvalCase / CaseResult / EvalResult dataclasses
- Built-in scorers: text_contains, tool_called, schema_valid, cost_under
- run_eval end-to-end
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Unit: ScriptedProvider
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scripted_provider_text_response():
    """ScriptedProvider with a text turn returns the configured text."""
    from linch.evals import ScriptedProvider, TextTurn
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
    from linch.evals import ScriptedProvider, ToolUseTurn
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
    from linch.evals import EvalCase

    case = EvalCase(prompt="What is 2+2?", expected="4")
    assert case.prompt == "What is 2+2?"
    assert case.expected == "4"


def test_case_result_dataclass():
    """CaseResult holds case + output + scorer verdicts."""
    from linch.evals import CaseResult, EvalCase

    case = EvalCase(prompt="hi", expected="hello")
    result = CaseResult(case=case, output="hello world", passed=True, scores={})
    assert result.passed is True
    assert result.output == "hello world"


def test_eval_result_dataclass():
    """EvalResult aggregates CaseResults with pass_rate."""
    from linch.evals import CaseResult, EvalCase, EvalResult

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
    from linch.evals import CaseResult, EvalCase, EvalResult
    from linch.events import ResultEvent, ToolCallStartEvent
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


def test_text_contains_scorer_pass():
    """text_contains scorer passes when expected substring is in output."""
    from linch.evals import text_contains

    scorer = text_contains("Paris")
    assert scorer("The capital is Paris.") is True


def test_text_contains_scorer_fail():
    from linch.evals import text_contains

    scorer = text_contains("Paris")
    assert scorer("The capital is London.") is False


def test_text_contains_case_insensitive():
    """text_contains is case-insensitive by default."""
    from linch.evals import text_contains

    scorer = text_contains("paris")
    assert scorer("The capital is PARIS.") is True


def test_tool_called_scorer_pass():
    """tool_called scorer passes when the expected tool name appears in events."""
    from linch.evals import tool_called
    from linch.events import ToolCallStartEvent

    scorer = tool_called("Read")
    events = [ToolCallStartEvent(tool_use_id="t1", tool_name="Read", input={}, summary="Read()")]
    assert scorer(events=events) is True


def test_tool_called_scorer_fail():
    from linch.evals import tool_called

    scorer = tool_called("Read")
    assert scorer(events=[]) is False


def test_schema_valid_scorer_pass():
    """schema_valid scorer passes when output is valid JSON matching the schema."""
    from linch.evals import schema_valid

    schema = {
        "type": "object",
        "properties": {"capital": {"type": "string"}},
        "required": ["capital"],
    }
    scorer = schema_valid(schema)
    assert scorer('{"capital": "Paris"}') is True


def test_schema_valid_scorer_fail_missing_key():
    from linch.evals import schema_valid

    schema = {
        "type": "object",
        "properties": {"capital": {"type": "string"}},
        "required": ["capital"],
    }
    scorer = schema_valid(schema)
    assert scorer('{"city": "Paris"}') is False


def test_schema_valid_scorer_fail_not_json():
    from linch.evals import schema_valid

    scorer = schema_valid({"type": "object"})
    assert scorer("not json at all") is False


def test_cost_under_scorer_pass():
    """cost_under scorer passes when total_cost_usd is below the budget."""
    from linch.evals import cost_under
    from linch.events import ResultEvent
    from linch.types import Usage

    scorer = cost_under(0.10)
    result_event = ResultEvent(
        subtype="success",
        stop_reason="end_turn",
        total_usage=Usage(),
        duration_ms=100,
        total_cost_usd=0.01,
    )
    assert scorer(result_event=result_event) is True


def test_cost_under_scorer_fail():
    from linch.evals import cost_under
    from linch.events import ResultEvent
    from linch.types import Usage

    scorer = cost_under(0.01)
    result_event = ResultEvent(
        subtype="success",
        stop_reason="end_turn",
        total_usage=Usage(),
        duration_ms=100,
        total_cost_usd=0.05,
    )
    assert scorer(result_event=result_event) is False


def test_cost_under_scorer_none_cost():
    """cost_under returns None (unknown) when total_cost_usd is None."""
    from linch.evals import cost_under
    from linch.events import ResultEvent
    from linch.types import Usage

    scorer = cost_under(0.10)
    result_event = ResultEvent(
        subtype="success",
        stop_reason="end_turn",
        total_usage=Usage(),
        duration_ms=100,
        total_cost_usd=None,
    )
    assert scorer(result_event=result_event) is None


def test_long_run_context_scorers():
    from linch.evals import context_metadata_contains, context_not_trimmed, context_selected_tool
    from linch.events import ContextBuildEvent

    events = [
        ContextBuildEvent(
            system_blocks=1,
            messages=2,
            selected_tools=["SearchMemory", "Read"],
            budget={"trimmed": False},
            metadata={"memory_namespace": "tenant-a"},
        )
    ]

    assert context_selected_tool("SearchMemory")(events=events) is True
    assert context_selected_tool("Bash")(events=events) is False
    assert context_not_trimmed()(events=events) is True
    assert context_metadata_contains("memory_namespace", "tenant-a")(events=events) is True


def test_memory_recalled_scorer():
    from linch.evals import memory_recalled
    from linch.events import ToolCallEndEvent
    from linch.tools import Citation, ToolResult

    events = [
        ToolCallEndEvent(
            tool_use_id="m1",
            tool_name="SearchMemory",
            tool_result=ToolResult(
                content="[policy-1] PTO rolls over",
                metadata={"result_ids": ["policy-1"]},
                citations=[Citation(id="policy-2", source="memory:tenant")],
            ),
        )
    ]

    assert memory_recalled(["policy-1", "policy-2"])(events=events) is True
    assert memory_recalled("missing")(events=events) is False
    assert memory_recalled("policy-1")(events=[]) is None


def test_recovery_and_completion_scorers():
    from linch.evals import recovery_succeeded, run_completed
    from linch.events import ResultEvent, ToolCallEndEvent
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
    from linch.evals import context_selected_tool, cost_under, memory_recalled, text_contains

    names = {
        text_contains("Paris").__name__,
        cost_under(0.05).__name__,
        context_selected_tool("SearchMemory").__name__,
        memory_recalled("pref-1").__name__,
    }

    assert names == {
        "text_contains:Paris",
        "cost_under:0.05",
        "context_selected_tool:SearchMemory",
        "memory_recalled:pref-1",
    }


# ---------------------------------------------------------------------------
# End-to-end: run_eval
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_eval_single_case_text_match():
    """run_eval with a single case and text_contains scorer returns EvalResult."""
    from linch import Agent
    from linch.evals import EvalCase, ScriptedProvider, TextTurn, run_eval, text_contains
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools

    provider = ScriptedProvider(turns=[TextTurn(text="The capital is Paris.")])
    agent = Agent(
        model="fake-model",
        provider=provider,
        tools=empty_tools(),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
    )

    cases = [EvalCase(prompt="What is the capital of France?", expected="Paris")]
    result = await run_eval(agent, cases, scorers=[text_contains("{expected}")])

    assert result.total == 1
    assert result.passed == 1
    assert result.pass_rate == 1.0


@pytest.mark.asyncio
async def test_run_eval_multiple_cases():
    """run_eval over multiple cases aggregates pass/fail correctly."""
    from linch import Agent
    from linch.evals import EvalCase, ScriptedProvider, TextTurn, run_eval, text_contains
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools

    # Provider always says "Paris" — first case passes, second fails
    provider = ScriptedProvider(turns=[TextTurn(text="Paris"), TextTurn(text="Paris")])
    agent = Agent(
        model="fake-model",
        provider=provider,
        tools=empty_tools(),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
    )

    cases = [
        EvalCase(prompt="Capital of France?", expected="Paris"),
        EvalCase(prompt="Capital of Germany?", expected="Berlin"),
    ]
    result = await run_eval(agent, cases, scorers=[text_contains("{expected}")])

    assert result.total == 2
    assert result.passed == 1
    assert result.pass_rate == 0.5


@pytest.mark.asyncio
async def test_run_eval_tool_called_scorer():
    """run_eval with tool_called scorer detects tool invocation."""
    from linch import Agent
    from linch.evals import (
        EvalCase,
        ScriptedProvider,
        TextTurn,
        ToolUseTurn,
        run_eval,
        tool_called,
    )
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import ToolRegistry

    class _EchoTool:
        name = "Echo"
        description = "Echoes input."
        input_schema = {"type": "object", "properties": {"text": {"type": "string"}}}
        scope = "read"
        parallel = True

        def validate(self, raw):
            return raw

        async def execute(self, input, ctx):
            from linch.tools.base import ToolResult

            return ToolResult(content=input.get("text", ""))

        def summarize(self, input):
            return "Echo()"

    reg = ToolRegistry()
    reg.register(_EchoTool())

    provider = ScriptedProvider(
        turns=[
            ToolUseTurn(tool_name="Echo", tool_input={"text": "hello"}),
            TextTurn(text="done"),
        ]
    )
    agent = Agent(
        model="fake-model",
        provider=provider,
        tools=reg,
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
    )

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
    from linch import Agent
    from linch.evals import EvalCase, ScriptedProvider, TextTurn, run_eval, schema_valid
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools

    provider = ScriptedProvider(turns=[TextTurn(text='{"capital": "Paris"}')])
    agent = Agent(
        model="fake-model",
        provider=provider,
        tools=empty_tools(),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
    )

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
    from linch import Agent
    from linch.evals import EvalCase, ScriptedProvider, TextTurn, run_eval
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools

    provider = ScriptedProvider(
        turns=[TextTurn(text="ok"), TextTurn(text="ok"), TextTurn(text="ok")]
    )
    agent = Agent(
        model="fake-model",
        provider=provider,
        tools=empty_tools(),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
    )

    baseline = len(agent._sessions)
    cases = [
        EvalCase(prompt="a", expected="ok"),
        EvalCase(prompt="b", expected="ok"),
        EvalCase(prompt="c", expected="ok"),
    ]
    await run_eval(agent, cases, scorers=[])

    assert len(agent._sessions) == baseline
