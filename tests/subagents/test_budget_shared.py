"""Shared-budget tests across the parent/subagent tree.

linch imports happen inside test functions / provider methods (not at module
level) because tests/loop/test_hardening.py pops all ``linch*`` modules from
``sys.modules`` — stale class references would fail ``isinstance`` checks in
the reloaded loop.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any


class ParentChildProvider:
    """Call 1: parent spawns a Subagent. Call 2: child text. Call 3: parent text."""

    id = "fake"

    def __init__(self, tokens_per_turn: int = 500) -> None:
        self.calls = 0
        self.tokens_per_turn = tokens_per_turn

    def context_window(self, model: str) -> int:
        return 10_000_000

    async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
        from linch.types import Usage

        self.calls += 1
        yield {"type": "message_start", "model": req.model}
        if self.calls == 1:
            yield {"type": "tool_use_start", "id": "call_1", "name": "Subagent"}
            yield {
                "type": "tool_use_input_delta",
                "id": "call_1",
                "json_delta": '{"description":"helper","prompt":"do the thing"}',
            }
            yield {"type": "tool_use_end", "id": "call_1"}
            stop_reason = "tool_use"
        else:
            yield {"type": "text_delta", "text": "done"}
            stop_reason = "end_turn"
        yield {
            "type": "message_end",
            "stop_reason": stop_reason,
            "usage": Usage(input_tokens=self.tokens_per_turn),
        }


def _make_agent(provider: Any, **kwargs: Any) -> Any:
    from linch import Agent
    from linch.sessions import InMemorySessionStore

    return Agent(
        model="gpt-5",
        provider=provider,
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        cwd=".",
        **kwargs,
    )


async def test_subagent_run_charges_parent_budget() -> None:
    from linch import RunBudget
    from linch.session import RunOptions

    provider = ParentChildProvider(tokens_per_turn=500)
    agent = _make_agent(provider)
    session = await agent.session()
    budget = RunBudget(max_tokens=100_000)

    events = [event async for event in session.run("delegate", RunOptions(budget=budget))]

    assert events[-1].type == "result"
    assert events[-1].subtype == "success"
    # Parent turn 1 + child turn + parent turn 2, all on one shared budget.
    assert provider.calls == 3
    assert budget.spent_tokens == 1500


async def test_exhausted_budget_stops_child_run() -> None:
    from linch import RunBudget
    from linch.subagents.default_agent import DEFAULT_AGENT
    from linch.subagents.runner import RunSubagentArgs, run_subagent
    from linch.types import Usage

    provider = ParentChildProvider()
    agent = _make_agent(provider)
    parent = await agent.session()
    budget = RunBudget(max_tokens=100)
    budget.charge(Usage(input_tokens=200), None)  # already exhausted
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
    assert provider.calls == 0  # child never reached the provider
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
