"""`Agent.eval` and `Agent.report` end to end against the real server."""

from __future__ import annotations

import pytest
from scripted import MODEL, config, text_turn

from linch_rust import (
    AgentNotFound,
    CaseResult,
    ProtocolError,
    RunNotFound,
    Server,
)

PRICING = {MODEL: {"input": 1.0, "output": 2.0}}
# The scripted turns report 3 input and 1 output token.
COST = 3 * 1e-6 + 1 * 2e-6


async def test_eval_scores_cases_and_reports_cost_tokens_and_duration() -> None:
    turns = [text_turn("It is Paris."), text_turn("Munich")]
    async with Server() as server:
        agent = await server.open_agent(config(turns, pricing=PRICING))
        report = await agent.eval(
            [
                {"prompt": "France?", "expected": "Paris"},
                {"prompt": "Germany?", "expected": "Berlin"},
            ],
            [{"type": "text_contains"}, {"type": "cost_under", "budget_usd": 1.0}],
        )
        assert (report["total"], report["passed"], report["pass_rate"]) == (2, 1, 0.5)
        assert report["usage"]["input_tokens"] == 6
        assert report["total_cost_usd"] == pytest.approx(2 * COST)
        assert report.get("markdown", "").startswith("# Linch Eval Report")
        first = report["cases"][0]
        assert first["scores"][1] == {"name": "cost_under:1.0", "verdict": True}
        assert first["total_cost_usd"] == pytest.approx(COST)
        assert first["usage"] is not None and first["duration_ms"] is not None

        one = await agent.report(str(first["run_id"]))
        assert one["report"]["status"] == "completed"
        assert one["report"]["summary"]["usage"]["input_tokens"] == 3
        assert one["report"]["timeline"] == []
        assert one["markdown"].startswith("# Linch Run Report")
        full = await agent.report(str(first["run_id"]), timeline=True)
        assert len(full["report"]["timeline"]) == full["report"]["event_count"]


async def test_a_custom_scorer_runs_in_python_and_changes_the_verdict() -> None:
    def short(result: CaseResult) -> bool:
        return len(result["output"]) < 5

    async def unsure(result: CaseResult) -> None:
        return None

    turns = [text_turn("ok"), text_turn("a long answer")]
    async with Server() as server:
        agent = await server.open_agent(config(turns))
        report = await agent.eval([{"prompt": "a"}, {"prompt": "b"}], [short, unsure])
        names = [score["name"] for score in report["cases"][0]["scores"]]
        assert names == ["short", "unsure"]
        assert [case["passed"] for case in report["cases"]] == [True, False]
        assert report["passed"] == 1 and report["pass_rate"] == 0.5
        assert "markdown" not in report

        def bad(result: CaseResult) -> int:
            return 1

        agent2 = await server.open_agent(config([text_turn("x")]))
        with pytest.raises(TypeError, match="bad"):
            await agent2.eval([{"prompt": "a"}], [bad])  # type: ignore[list-item]


async def test_bad_input_is_refused_with_a_typed_error() -> None:
    async with Server() as server:
        agent = await server.open_agent(config([text_turn("x")]))
        with pytest.raises(ProtocolError):
            await agent.eval([{"prompt": ""}])
        with pytest.raises(ProtocolError):
            await agent.eval([{"prompt": "a"}], [{"type": "no_such"}])
        with pytest.raises(RunNotFound):
            await agent.report("no-such-run")
        await agent.close()
        with pytest.raises(AgentNotFound):
            await agent.eval([{"prompt": "a"}])
