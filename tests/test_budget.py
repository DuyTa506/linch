"""Budget primitive tests.

linch imports happen inside test functions / provider methods (not at module
level) because tests/loop/test_hardening.py pops all ``linch*`` modules from
``sys.modules`` — stale class references would fail ``isinstance`` checks in
the reloaded loop (e.g. the ``Usage`` guard in ``stream_turn``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

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


def test_remaining_and_exceeded_token_limit() -> None:
    from linch import RunBudget
    from linch.types import Usage

    budget = RunBudget(max_tokens=1000)

    assert budget.remaining_tokens == 1000
    assert budget.remaining_usd is None
    assert not budget.exceeded

    budget.charge(Usage(input_tokens=1200), None)

    assert budget.remaining_tokens == 0
    assert budget.exceeded


def test_exceeded_usd_limit() -> None:
    from linch import RunBudget
    from linch.types import Usage

    budget = RunBudget(max_cost_usd=1.0)

    budget.charge(Usage(input_tokens=10), 0.4)
    assert not budget.exceeded
    assert budget.remaining_usd == 0.6

    budget.charge(Usage(input_tokens=10), 0.7)
    assert budget.exceeded
    assert budget.remaining_usd == 0.0


def test_no_limits_never_exceeds() -> None:
    from linch import RunBudget
    from linch.types import Usage

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


class ToolLoopProvider:
    """Always requests a tool call, reporting fixed usage per turn."""

    id = "fake"

    def __init__(self, tokens_per_turn: int = 600) -> None:
        self.calls = 0
        self.tokens_per_turn = tokens_per_turn

    def context_window(self, model: str) -> int:
        return 10_000_000

    async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
        from linch.types import Usage

        self.calls += 1
        yield {"type": "message_start", "model": req.model}
        yield {"type": "tool_use_start", "id": f"call_{self.calls}", "name": "Glob"}
        yield {
            "type": "tool_use_input_delta",
            "id": f"call_{self.calls}",
            "json_delta": f'{{"pattern":"*.txt{self.calls}"}}',
        }
        yield {"type": "tool_use_end", "id": f"call_{self.calls}"}
        yield {
            "type": "message_end",
            "stop_reason": "tool_use",
            "usage": Usage(input_tokens=self.tokens_per_turn),
        }


class UsageScriptProvider:
    """Scripted (stop_reason, usage) turns; tool_use turns call Glob."""

    id = "fake"

    def __init__(self, turns: list[tuple[str, int]]) -> None:
        self.calls = 0
        self.turns = turns

    def context_window(self, model: str) -> int:
        return 10_000_000

    async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
        from linch.types import Usage

        stop_reason, tokens = self.turns[self.calls]
        self.calls += 1
        yield {"type": "message_start", "model": req.model}
        if stop_reason == "tool_use":
            yield {"type": "tool_use_start", "id": f"call_{self.calls}", "name": "Glob"}
            yield {
                "type": "tool_use_input_delta",
                "id": f"call_{self.calls}",
                "json_delta": f'{{"pattern":"*.txt{self.calls}"}}',
            }
            yield {"type": "tool_use_end", "id": f"call_{self.calls}"}
        else:
            yield {"type": "text_delta", "text": "done"}
        yield {
            "type": "message_end",
            "stop_reason": stop_reason,
            "usage": Usage(input_tokens=tokens),
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


async def test_loop_stops_when_token_budget_exhausted() -> None:
    from linch import RunBudget
    from linch.session import RunOptions

    provider = ToolLoopProvider(tokens_per_turn=600)
    agent = _make_agent(provider)
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
    assert provider.calls == 2
    assert budget.exceeded
    assert budget.spent_tokens == 1200


async def test_warning_event_emitted_once_at_90_percent() -> None:
    from linch import RunBudget
    from linch.session import RunOptions

    provider = UsageScriptProvider([("tool_use", 9500), ("end_turn", 400)])
    agent = _make_agent(provider)
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
    from linch import RunBudget
    from linch.session import RunOptions

    agent_budget = RunBudget(max_tokens=10_000)
    provider = UsageScriptProvider([("end_turn", 100), ("end_turn", 100)])
    agent = _make_agent(provider, budget=agent_budget)

    session = await agent.session()
    async for _ in session.run("go"):
        pass
    assert agent_budget.spent_tokens == 100  # agent-level fallback charged

    run_budget = RunBudget(max_tokens=10_000)
    session2 = await agent.session()
    async for _ in session2.run("go", RunOptions(budget=run_budget)):
        pass
    assert run_budget.spent_tokens == 100  # RunOptions wins
    assert agent_budget.spent_tokens == 100  # agent budget untouched on run 2


async def test_budget_queryable_after_run() -> None:
    from linch import RunBudget
    from linch.session import RunOptions

    provider = UsageScriptProvider([("end_turn", 250)])
    agent = _make_agent(provider)
    session = await agent.session()
    budget = RunBudget(max_tokens=1000)

    async for _ in session.run("go", RunOptions(budget=budget)):
        pass

    assert budget.spent_tokens == 250
    assert budget.remaining_tokens == 750
    assert session.active_budget is budget
