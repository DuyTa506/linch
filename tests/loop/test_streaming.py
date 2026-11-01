"""Primary deliveries distinguish durable intent from physical dispatch."""

from typing import Any

import pytest

from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn


@pytest.mark.parametrize("stop", ["abort", "budget"])
async def test_primary_recheck_after_dispatch_marker_abandons_unsent_call(stop: str) -> None:
    from linch import RunBudget
    from linch.abort import AbortContext
    from linch.errors import AbortError
    from linch.loop.streaming import _accounted_primary_stream
    from linch.model_calls import _ModelCallError, _ModelCallLedger
    from linch.types import ProviderRequest, Usage

    signal, budget = AbortContext(), RunBudget(max_tokens=100)
    journal: list[Any] = []

    async def persist(event: Any) -> None:
        journal.append(event)
        if event.record_kind == "dispatch_started":
            if stop == "abort":
                signal.abort()
            else:
                budget.charge(Usage(100), None)

    provider = ScriptedProvider([TextTurn("must not be dispatched")])
    agent = make_agent(provider)
    session = await agent.session()
    ledger = _ModelCallLedger("run", budget=budget, persist=persist)
    request = ProviderRequest("test-model", [], [], [], signal=signal)
    with pytest.raises(AbortError if stop == "abort" else _ModelCallError):
        _ = [
            item
            async for item in _accounted_primary_stream(
                session,
                request,
                ledger=ledger,
                logical_call_id="primary",
                turn_index=0,
                bind_active_primary=None,
            )
        ]

    assert provider.requests == []
    assert [event.record_kind for event in journal] == [
        "intent",
        "dispatch_started",
        "abandoned",
    ]
    assert journal[-1].record["usage_status"] == "not_dispatched"
    assert ledger.receipts() == []
    restored = _ModelCallLedger("run", budget=RunBudget(max_tokens=100))
    await restored.restore(journal)
    restored.assert_may_start()
