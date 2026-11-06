from __future__ import annotations

import pytest

# ── RunBudget unit tests ──────────────────────────────────────────────────────


def test_charge_accumulates_tokens_and_cost() -> None:
    from linch import RunBudget
    from linch.types import Usage

    budget = RunBudget(max_tokens=10_000)

    budget.charge(
        Usage(input_tokens=100, output_tokens=50, cache_read_tokens=25, cache_creation_tokens=25),
        0.01,
    )
    budget.charge(Usage(input_tokens=300), None)

    assert budget.spent_tokens == 500
    assert budget.spent_usd == 0.01  # None cost charges 0


@pytest.mark.parametrize("case", ["tokens", "usd", "unlimited"])
def test_budget_limits(case: str) -> None:
    from linch import RunBudget
    from linch.types import Usage

    if case == "tokens":
        budget = RunBudget(max_tokens=1000)
        assert budget.remaining_tokens == 1000
        assert budget.remaining_usd is None
        assert not budget.exceeded
        budget.charge(Usage(input_tokens=1200), None)
        assert budget.remaining_tokens == 0
        assert budget.exceeded
    elif case == "usd":
        budget = RunBudget(max_cost_usd=1.0)
        budget.charge(Usage(input_tokens=10), 0.4)
        assert not budget.exceeded
        assert budget.remaining_usd == 0.6
        budget.charge(Usage(input_tokens=10), 0.7)
        assert budget.exceeded
        assert budget.remaining_usd == 0.0
    else:
        budget = RunBudget()
        budget.charge(Usage(input_tokens=10**9), 10**6)
        assert not budget.exceeded
        assert budget.remaining_tokens is None
        assert budget.remaining_usd is None


def test_take_warning_fires_once_at_ratio() -> None:
    from linch import RunBudget
    from linch.types import Usage

    budget = RunBudget(max_tokens=1000)

    budget.charge(Usage(input_tokens=500), None)
    assert budget.take_warning() is False

    budget.charge(Usage(input_tokens=450), None)  # 950/1000 ≥ 0.9
    assert budget.take_warning() is True
    assert budget.take_warning() is False  # once per budget object


# ── Loop integration ──────────────────────────────────────────────────────────


async def test_loop_stops_when_token_budget_exhausted() -> None:
    from linch import RunBudget, RunOptions
    from linch.types import Usage
    from tests.support.agents import make_agent
    from tests.support.providers import ScriptedProvider, ToolUseTurn
    from tests.support.tools import StubTool, tool_registry

    provider = ScriptedProvider(
        [
            ToolUseTurn("Glob", {"pattern": "*.txt1"}, usage=Usage(input_tokens=600)),
            ToolUseTurn("Glob", {"pattern": "*.txt2"}, usage=Usage(input_tokens=600)),
        ]
    )
    agent = make_agent(
        provider, model="gpt-5", tools=tool_registry(StubTool("Glob")), run_store=None
    )
    session = await agent.session()
    budget = RunBudget(max_tokens=1000)

    events = [event async for event in session.run("go", RunOptions(budget=budget))]

    budget_events = [e for e in events if e.type == "budget"]
    assert [e.kind for e in budget_events].count("exceeded") == 1
    error_events = [e for e in events if e.type == "error"]
    assert any(e.error.get("name") == "BudgetExceededError" for e in error_events)
    assert events[-1].type == "result"
    assert events[-1].subtype == "error"
    # Turn 1 charges 600, turn 2 charges 1200 (exceeded), turn 3 never calls the provider.
    assert len(provider.requests) == 2
    assert budget.exceeded
    assert budget.spent_tokens == 1200


async def test_warning_event_emitted_once_at_90_percent() -> None:
    from linch import RunBudget, RunOptions
    from linch.types import Usage
    from tests.support.agents import make_agent
    from tests.support.providers import ScriptedProvider, TextTurn, ToolUseTurn
    from tests.support.tools import StubTool, tool_registry

    provider = ScriptedProvider(
        [
            ToolUseTurn("Glob", {"pattern": "*.txt"}, usage=Usage(input_tokens=9500)),
            TextTurn("done", usage=Usage(input_tokens=400)),
        ]
    )
    agent = make_agent(
        provider, model="gpt-5", tools=tool_registry(StubTool("Glob")), run_store=None
    )
    session = await agent.session()
    budget = RunBudget(max_tokens=10_000)

    events = [event async for event in session.run("go", RunOptions(budget=budget))]

    warnings = [e for e in events if e.type == "budget" and e.kind == "warning"]
    assert len(warnings) == 1
    assert warnings[0].spent_tokens == 9500
    assert warnings[0].max_tokens == 10_000
    assert events[-1].type == "result"
    assert events[-1].subtype == "success"


async def test_agent_level_budget_fallback_and_runoptions_precedence() -> None:
    from linch import RunBudget, RunOptions
    from linch.types import Usage
    from tests.support.agents import make_agent
    from tests.support.providers import ScriptedProvider, TextTurn

    agent_budget = RunBudget(max_tokens=10_000)
    provider = ScriptedProvider([TextTurn("done", usage=Usage(input_tokens=100))] * 2)
    agent = make_agent(provider, model="gpt-5", budget=agent_budget, run_store=None)

    session = await agent.session()
    async for _ in session.run("go"):
        pass
    assert agent_budget.spent_tokens == 100  # agent-level fallback charged

    run_budget = RunBudget(max_tokens=10_000)
    session2 = await agent.session()
    async for _ in session2.run("go", RunOptions(budget=run_budget)):
        pass
    assert run_budget.spent_tokens == 100  # RunOptions wins
    assert run_budget.remaining_tokens == 9_900
    assert session2.active_budget is run_budget  # the caller's object, not a copy
    assert agent_budget.spent_tokens == 100  # agent budget untouched on run 2
