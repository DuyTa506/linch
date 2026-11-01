from __future__ import annotations

import pytest


def test_normalized_usage_charges_cache_tokens_once_and_prices_each_bucket() -> None:
    from linch import RunBudget
    from linch.pricing import ModelPricing, cost_usd
    from linch.types import Usage

    usage = Usage(input_tokens=100, output_tokens=10, cache_read_tokens=900)
    pricing = ModelPricing(input=1.0, output=2.0, cache_read=0.1, cache_creation=1.0)
    cost = cost_usd(usage, "test-model", {"test-model": pricing})
    assert cost == pytest.approx((100 + 20 + 90) / 1_000_000)

    budget = RunBudget(max_tokens=1500)
    budget.charge(usage, cost)

    assert budget.spent_tokens == 1010
    assert budget.spent_usd == pytest.approx(cost)
    assert not budget.exceeded


@pytest.mark.asyncio
async def test_receipt_restore_does_not_charge_normalized_usage_twice() -> None:
    from linch import RunBudget
    from linch.model_calls import ModelCallIntent, ModelCallReceipt, _ModelCallLedger
    from linch.types import Usage

    usage = Usage(input_tokens=100, output_tokens=10, cache_read_tokens=900)
    budget = RunBudget(max_tokens=1500)
    events = []

    async def persist(event) -> None:
        events.append(event)

    ledger = _ModelCallLedger("run", budget=budget, persist=persist)
    intent = ModelCallIntent(
        logical_call_id="logical",
        delivery_attempt_id="delivery",
        parent_logical_call_id=None,
        kind="primary",
        run_id="run",
        turn_index=0,
        requested_model="test-model",
        request_fingerprint="request",
        strategy_id=None,
    )
    receipt = ModelCallReceipt(
        logical_call_id="logical",
        delivery_attempt_id="delivery",
        kind="primary",
        run_id="run",
        turn_index=0,
        actual_model="test-model",
        usage_status="reported",
        usage=usage,
        cost_usd=None,
        disposition="accepted",
        rejection_reason=None,
        accepted_text=None,
        accepted_text_sha256=None,
        duration_ms=0,
        request_fingerprint="request",
        strategy_id=None,
        source_context_fingerprint=None,
    )
    await ledger.begin(intent)
    await ledger.mark_dispatch_started(intent)
    await ledger.settle(receipt)
    assert budget.spent_tokens == 1010

    await ledger.restore(events)

    assert ledger.total_usage == usage
    assert budget.spent_tokens == 1010
