from __future__ import annotations

from typing import Any

from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn, ToolUseTurn


def _make_agent(provider: Any, **kwargs: Any) -> Any:
    from linch import workspace_tools
    from linch.config import FeatureFlags

    return make_agent(
        provider,
        model="gpt-5",
        tools=workspace_tools(),
        features=FeatureFlags(subagents=True),
        **kwargs,
    )


async def test_subagent_run_charges_parent_budget() -> None:
    from linch import RunBudget, RunOptions
    from linch.types import Usage

    provider = ScriptedProvider(
        [
            ToolUseTurn(
                "Subagent",
                {"description": "helper", "prompt": "do the thing"},
                usage=Usage(input_tokens=500),
            ),
            TextTurn("done", usage=Usage(input_tokens=500)),
            TextTurn("done", usage=Usage(input_tokens=500)),
        ]
    )
    agent = _make_agent(provider)
    session = await agent.session()
    budget = RunBudget(max_tokens=100_000)

    events = [event async for event in session.run("delegate", RunOptions(budget=budget))]
    assert events[-1].type == "result"
    assert events[-1].subtype == "success"
    assert len(provider.requests) == 3
    assert budget.spent_tokens == 1500


async def test_budget_aggregates_across_three_levels() -> None:
    from linch import RunBudget, RunOptions
    from linch.types import Usage

    provider = ScriptedProvider(
        [
            ToolUseTurn(
                "Subagent",
                {"description": "helper", "prompt": "go deeper"},
                usage=Usage(input_tokens=300),
            ),
            ToolUseTurn(
                "Subagent",
                {"description": "helper", "prompt": "go deeper"},
                usage=Usage(input_tokens=300),
            ),
            TextTurn("done", usage=Usage(input_tokens=300)),
            TextTurn("done", usage=Usage(input_tokens=300)),
            TextTurn("done", usage=Usage(input_tokens=300)),
        ]
    )
    agent = _make_agent(provider)
    session = await agent.session()
    budget = RunBudget(max_tokens=100_000)

    events = [event async for event in session.run("delegate", RunOptions(budget=budget))]
    assert events[-1].type == "result"
    assert events[-1].subtype == "success"
    assert len(provider.requests) >= 3
    assert budget.spent_tokens == len(provider.requests) * 300


async def test_exhausted_budget_stops_child_run() -> None:
    from linch import RunBudget
    from linch.subagents.default_agent import DEFAULT_AGENT
    from linch.subagents.runner import RunSubagentArgs, run_subagent
    from linch.types import Usage

    provider = ScriptedProvider(
        [ToolUseTurn("Subagent", {"description": "helper", "prompt": "do the thing"})]
    )
    agent = _make_agent(provider)
    parent = await agent.session()
    budget = RunBudget(max_tokens=100)
    budget.charge(Usage(input_tokens=200), None)
    parent.active_budget = budget

    emitted: list[Any] = []
    result = await run_subagent(
        RunSubagentArgs(
            parent_session=parent,
            parent_agent=agent,
            definition=DEFAULT_AGENT,
            prompt="do the thing",
            display_name="helper",
            subagent_run_id="sa_test",
            emit=emitted.append,
        )
    )

    assert result.errored
    assert result.error is not None
    assert result.error["name"] == "BudgetExceededError"
    assert provider.requests == []
    nested = [e.event for e in emitted]
    assert any(e.type == "budget" and e.kind == "exceeded" for e in nested)


def test_budget_event_round_trips_event_dict() -> None:
    from linch.events import BudgetEvent, event_from_dict, event_to_dict

    event = BudgetEvent(
        kind="exceeded",
        spent_tokens=1200,
        spent_usd=0.5,
        max_tokens=1000,
        max_cost_usd=None,
    )

    restored = event_from_dict(event_to_dict(event))

    assert restored == event
