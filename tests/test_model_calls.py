"""Receipt accounting contracts, with no provider or live service dependencies."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from typing import Any

import pytest


def _records(**overrides: Any) -> tuple[Any, Any]:
    from linch.model_calls import ModelCallIntent, ModelCallReceipt
    from linch.types import Usage

    intent = ModelCallIntent(
        logical_call_id="logical-fixture",
        delivery_attempt_id="delivery-fixture",
        parent_logical_call_id=None,
        kind="compaction",
        run_id="run-fixture",
        turn_index=2,
        requested_model="gpt-5",
        request_fingerprint="request-fingerprint-fixture",
        strategy_id="default-v1",
    )
    receipt = ModelCallReceipt(
        logical_call_id=intent.logical_call_id,
        delivery_attempt_id=intent.delivery_attempt_id,
        kind=intent.kind,
        run_id=intent.run_id,
        turn_index=intent.turn_index,
        actual_model="gpt-5",
        usage_status="reported",
        usage=Usage(input_tokens=111, output_tokens=22),
        cost_usd=0.00035875,
        disposition="accepted",
        rejection_reason=None,
        accepted_text="Paid summary.",
        accepted_text_sha256="433e0a6d4433fc41dbf852b981cb494351af8906bc10b9af4fdbb4779da76346",
        duration_ms=17,
        request_fingerprint=intent.request_fingerprint,
        strategy_id=intent.strategy_id,
        source_context_fingerprint="source-fingerprint-fixture",
    )
    return intent, replace(receipt, **overrides) if overrides else receipt


async def _settled(*, budget: Any = None, persist: Any = None) -> Any:
    from linch.model_calls import _ModelCallLedger

    intent, receipt = _records()
    ledger = _ModelCallLedger(intent.run_id, budget=budget, persist=persist)
    await ledger.begin(intent)
    await ledger.mark_dispatch_started(intent)
    assert await ledger.settle(receipt) is True
    return ledger


def test_strict_codec_roundtrip() -> None:
    from linch.model_calls import (
        AuxiliaryTextResult,
        intent_from_dict,
        intent_to_dict,
        receipt_from_dict,
        receipt_to_dict,
    )

    intent, receipt = _records()
    encoded = receipt_to_dict(receipt)
    assert encoded["version"] == 1
    assert encoded["usage"] == {
        "input_tokens": 111,
        "output_tokens": 22,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
    }
    assert intent_from_dict(intent_to_dict(intent)) == intent
    assert receipt_from_dict(encoded) == receipt
    assert AuxiliaryTextResult(receipt, receipt.accepted_text).text == "Paid summary."
    with pytest.raises(FrozenInstanceError):
        receipt.actual_model = "changed"
    assert not hasattr(intent, "__dict__")


@pytest.mark.parametrize("record_kind", ["intent", "receipt"])
def test_unknown_required_version_fails_closed(record_kind: str) -> None:
    from linch.model_calls import (
        _ModelCallError,
        intent_from_dict,
        intent_to_dict,
        receipt_from_dict,
        receipt_to_dict,
    )

    intent, receipt = _records()
    raw = intent_to_dict(intent) if record_kind == "intent" else receipt_to_dict(receipt)
    raw["version"] = 2
    with pytest.raises(_ModelCallError):
        (intent_from_dict if record_kind == "intent" else receipt_from_dict)(raw)


@pytest.mark.parametrize("value", [True, -1, 1.5, "111", float("nan"), float("inf")])
def test_invalid_usage_is_rejected(value: Any) -> None:
    from linch.model_calls import _ModelCallError, receipt_from_dict, receipt_to_dict

    _, receipt = _records()
    raw = receipt_to_dict(receipt)
    raw["usage"]["input_tokens"] = value
    with pytest.raises(_ModelCallError):
        receipt_from_dict(raw)


@pytest.mark.parametrize("value", [True, -1, "0.2", float("nan"), float("inf")])
def test_invalid_cost_is_rejected(value: Any) -> None:
    from linch.model_calls import _ModelCallError, receipt_from_dict, receipt_to_dict

    _, receipt = _records()
    raw = receipt_to_dict(receipt)
    raw["cost_usd"] = value
    with pytest.raises(_ModelCallError):
        receipt_from_dict(raw)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("accepted_text_sha256", "0" * 64),
        ("accepted_text_sha256", None),
        ("disposition", "future-disposition"),
        ("usage_status", "free"),
        ("actual_model", ""),
        ("duration_ms", True),
        ("kind", "other"),
        ("turn_index", -1),
        ("request_fingerprint", ""),
        ("accepted_text", "x" * (1024 * 1024 + 1)),
    ],
    ids=[
        "hash",
        "missing-hash",
        "disposition",
        "usage-status",
        "actual-model",
        "duration",
        "kind",
        "turn",
        "fingerprint",
        "oversize",
    ],
)
def test_invalid_receipt_or_hash_fails_closed(field: str, value: Any) -> None:
    from linch.model_calls import _ModelCallError, receipt_from_dict, receipt_to_dict

    _, receipt = _records()
    raw = receipt_to_dict(receipt)
    raw[field] = value
    if field == "accepted_text":
        import hashlib

        raw["accepted_text_sha256"] = hashlib.sha256(value.encode("utf-8")).hexdigest()
    with pytest.raises(_ModelCallError):
        receipt_from_dict(raw)


def test_missing_required_fields_fail_closed() -> None:
    from linch.model_calls import (
        _ModelCallError,
        intent_from_dict,
        intent_to_dict,
        receipt_from_dict,
        receipt_to_dict,
    )

    intent, receipt = _records()
    raw = intent_to_dict(intent)
    del raw["parent_logical_call_id"]
    with pytest.raises(_ModelCallError):
        intent_from_dict(raw)
    raw = receipt_to_dict(receipt)
    del raw["usage"]["cache_creation_tokens"]
    with pytest.raises(_ModelCallError):
        receipt_from_dict(raw)


async def test_settlement_charges_once_by_delivery_id() -> None:
    from linch.budget import RunBudget
    from linch.model_calls import _ModelCallLedger

    intent, receipt = _records()
    budget = RunBudget(max_tokens=1000)
    ledger = _ModelCallLedger(intent.run_id, budget=budget)
    await ledger.begin(intent)
    await ledger.mark_dispatch_started(intent)
    assert await ledger.settle(receipt) is True
    assert await ledger.settle(receipt) is False
    assert ledger.total_usage.input_tokens == 111
    assert ledger.total_usage.output_tokens == 22
    assert budget.spent_tokens == 133
    assert budget.spent_usd == 0.00035875
    next_intent = replace(intent, delivery_attempt_id="delivery-second")
    await ledger.begin(next_intent)
    await ledger.mark_dispatch_started(next_intent)
    await ledger.settle(replace(receipt, delivery_attempt_id="delivery-second"))
    assert ledger.total_usage.input_tokens == 222
    assert budget.spent_tokens == 266


async def test_receipt_without_started_intent_fails_closed() -> None:
    from linch.model_calls import _ModelCallError, _ModelCallLedger

    intent, receipt = _records()
    ledger = _ModelCallLedger(intent.run_id)
    with pytest.raises(_ModelCallError):
        await ledger.settle(receipt)
    await ledger.begin(intent)
    with pytest.raises(_ModelCallError):
        await ledger.settle(receipt)
    assert ledger.total_usage.input_tokens == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [("logical_call_id", "other"), ("request_fingerprint", "other"), ("turn_index", 7)],
)
async def test_mismatched_intent_receipt_identity_fails_closed(field: str, value: Any) -> None:
    from linch.model_calls import _ModelCallError, _ModelCallLedger

    intent, receipt = _records()
    ledger = _ModelCallLedger(intent.run_id)
    await ledger.begin(intent)
    await ledger.mark_dispatch_started(intent)
    with pytest.raises(_ModelCallError):
        await ledger.settle(replace(receipt, **{field: value}))
    assert ledger.total_usage.input_tokens == 0


async def test_conflicting_duplicate_receipt_fails_closed() -> None:
    from linch.model_calls import _ModelCallError

    ledger = await _settled()
    _, receipt = _records(cost_usd=1.0)
    with pytest.raises(_ModelCallError):
        await ledger.settle(receipt)
    assert ledger.total_cost_usd == 0.00035875


async def test_caller_usage_mutation_cannot_change_owned_spend() -> None:
    from linch.model_calls import _ModelCallLedger
    from linch.types import Usage

    usage = Usage(111, 22)
    intent, receipt = _records(usage=usage)
    usage.input_tokens = 900
    assert receipt.usage.input_tokens == 111
    ledger = _ModelCallLedger(intent.run_id)
    await ledger.begin(intent)
    await ledger.mark_dispatch_started(intent)
    await ledger.settle(receipt)
    receipt.usage.output_tokens = 800
    assert ledger.total_usage.output_tokens == 22
    assert ledger.snapshot()["receipts"][0]["usage"]["output_tokens"] == 22


async def test_snapshot_and_total_usage_are_detached() -> None:
    ledger = await _settled()
    snapshot = ledger.snapshot()
    snapshot["receipts"][0]["usage"]["input_tokens"] = 999
    snapshot["total_usage"]["output_tokens"] = 999
    snapshot["settled_delivery_ids"].clear()
    ledger.total_usage.input_tokens = 999
    assert ledger.total_usage.input_tokens == 111
    assert ledger.snapshot()["total_usage"]["output_tokens"] == 22
    assert ledger.snapshot()["settled_delivery_ids"] == ["delivery-fixture"]


async def test_same_budget_owner_resume_does_not_charge_again() -> None:
    from linch.budget import RunBudget
    from linch.model_calls import _ModelCallLedger

    budget = RunBudget(max_tokens=1000)
    original = await _settled(budget=budget)
    resumed = _ModelCallLedger("run-fixture", budget=budget)
    await resumed.restore(original.snapshot(), [])
    assert budget.spent_tokens == 133
    assert resumed.total_usage.input_tokens == 111


async def test_fresh_zero_budget_adopts_owner_and_restores_spend_once() -> None:
    from linch.budget import RunBudget
    from linch.model_calls import _ModelCallLedger

    snapshot = (await _settled()).snapshot()
    budget = RunBudget(max_tokens=1000)
    ledger = _ModelCallLedger("run-fixture", budget=budget)
    await ledger.restore(snapshot, [])
    await ledger.restore(snapshot, [])
    assert budget.spent_tokens == 133
    assert ledger.snapshot()["budget_owner_id"] == snapshot["budget_owner_id"]


async def test_fresh_nonzero_or_mismatched_capped_budget_owner_fails_closed() -> None:
    from linch.budget import RunBudget
    from linch.errors import ConfigError
    from linch.model_calls import _ModelCallLedger

    snapshot = (await _settled()).snapshot()
    nonzero = RunBudget(max_tokens=1000, spent_tokens=1)
    with pytest.raises(ConfigError):
        await _ModelCallLedger("run-fixture", budget=nonzero).restore(snapshot, [])
    other = RunBudget(max_tokens=1000)
    await _settled(budget=other)
    with pytest.raises(ConfigError):
        await _ModelCallLedger("run-fixture", budget=other).restore(snapshot, [])


async def test_same_owner_missing_ids_or_inconsistent_counters_fail_closed() -> None:
    from linch.budget import RunBudget
    from linch.errors import ConfigError
    from linch.model_calls import _ModelCallLedger

    budget = RunBudget(max_tokens=1000)
    ledger = await _settled(budget=budget)
    snapshot = ledger.snapshot()
    budget.spent_tokens = 0
    with pytest.raises(ConfigError):
        await _ModelCallLedger("run-fixture", budget=budget).restore(snapshot, [])
    budget.spent_tokens = 133
    budget._charged_delivery_ids.clear()
    with pytest.raises(ConfigError):
        await _ModelCallLedger("run-fixture", budget=budget).restore(snapshot, [])


async def test_same_owner_accepts_extra_live_sibling_charges() -> None:
    from linch.budget import RunBudget
    from linch.model_calls import _ModelCallLedger

    budget = RunBudget(max_tokens=1000)
    ledger = await _settled(budget=budget)
    snapshot = ledger.snapshot()
    intent, receipt = _records()
    intent = replace(intent, run_id="sibling", delivery_attempt_id="sibling-delivery")
    receipt = replace(receipt, run_id="sibling", delivery_attempt_id="sibling-delivery")
    sibling = _ModelCallLedger("sibling", budget=budget)
    await sibling.begin(intent)
    await sibling.mark_dispatch_started(intent)
    await sibling.settle(receipt)
    await _ModelCallLedger("run-fixture", budget=budget).restore(snapshot, [])
    assert budget.spent_tokens == 266


async def test_journal_write_failure_retains_observed_spend_and_blocks_continuation() -> None:
    from linch.budget import RunBudget
    from linch.model_calls import _ModelCallError

    persisted = []

    async def persist(event: Any) -> None:
        if event.record_kind == "receipt":
            raise OSError("storage unavailable")
        persisted.append(event)

    budget = RunBudget(max_tokens=1000)
    with pytest.raises(_ModelCallError):
        await _settled(budget=budget, persist=persist)
    assert budget.spent_tokens == 133
    from linch.model_calls import _ModelCallLedger

    intent, receipt = _records()
    ledger = _ModelCallLedger(intent.run_id, budget=RunBudget(), persist=persist)
    await ledger.begin(intent)
    await ledger.mark_dispatch_started(intent)
    with pytest.raises(_ModelCallError):
        await ledger.settle(receipt)
    assert ledger.total_usage.input_tokens == 111
    assert ledger.snapshot()["settled_delivery_ids"] == []
    with pytest.raises(_ModelCallError):
        ledger.assert_may_start()
    with pytest.raises(_ModelCallError):
        await ledger.settle(receipt)
    assert ledger.total_usage.input_tokens == 111


@pytest.mark.parametrize("capped", [True, False])
async def test_sibling_admission_after_unknown_dispatched_use(capped: bool) -> None:
    from linch.budget import RunBudget
    from linch.model_calls import _ModelCallError, _ModelCallLedger

    budget = RunBudget(max_tokens=1000 if capped else None)
    intent, _ = _records()
    child = _ModelCallLedger(intent.run_id, budget=budget)
    sibling = _ModelCallLedger("sibling", budget=budget)
    await child.begin(intent)
    await child.mark_dispatch_started(intent)
    await child.abandon(intent, reason="cancelled")
    assert child.usage_complete is False
    assert child.total_usage.input_tokens == 0
    assert budget.spent_tokens == 0
    if capped:
        with pytest.raises(_ModelCallError):
            sibling.assert_may_start()
    else:
        sibling.assert_may_start()


async def test_intent_only_recovery_is_not_dispatched_and_does_not_fence_budget() -> None:
    from linch.budget import RunBudget
    from linch.model_calls import _ModelCallLedger

    intent, _ = _records()
    ledger = _ModelCallLedger(intent.run_id)
    await ledger.begin(intent)
    budget = RunBudget(max_tokens=1000)
    restored = _ModelCallLedger(intent.run_id, budget=budget)
    await restored.restore(ledger.snapshot(), [])
    restored.assert_may_start()
    assert restored.usage_complete is True
    await restored.abandon(intent, reason="pre-dispatch cancellation")
    restored.assert_may_start()
    assert restored.usage_complete is True


async def test_started_recovery_is_unknown_and_fences_fresh_capped_budget() -> None:
    from linch.budget import RunBudget
    from linch.model_calls import _ModelCallError, _ModelCallLedger

    intent, _ = _records()
    ledger = _ModelCallLedger(intent.run_id)
    await ledger.begin(intent)
    await ledger.mark_dispatch_started(intent)
    restored = _ModelCallLedger(intent.run_id, budget=RunBudget(max_tokens=1000))
    await restored.restore(ledger.snapshot(), [])
    assert restored.usage_complete is False
    with pytest.raises(_ModelCallError):
        restored.assert_may_start()


async def test_journal_ahead_checkpoint_reconciles_by_delivery_without_repricing() -> None:
    from linch.budget import RunBudget
    from linch.model_calls import _ModelCallLedger

    journal = []

    async def persist(event: Any) -> None:
        journal.append(event)

    intent, receipt = _records()
    original = _ModelCallLedger(intent.run_id, persist=persist)
    await original.begin(intent)
    checkpoint = original.snapshot()
    await original.mark_dispatch_started(intent)
    await original.settle(receipt)
    budget = RunBudget(max_tokens=1000)
    restored = _ModelCallLedger(intent.run_id, budget=budget)
    await restored.restore(checkpoint, journal)
    assert restored.total_usage.input_tokens == 111
    assert restored.total_cost_usd == 0.00035875
    assert restored.usage_complete is True
    assert budget.spent_tokens == 133
    await restored.restore(restored.snapshot(), journal)
    assert budget.spent_tokens == 133


async def test_unknown_pricing_keeps_known_cost_sum_partial() -> None:
    from linch.model_calls import _ModelCallLedger

    intent, receipt = _records(cost_usd=None)
    ledger = _ModelCallLedger(intent.run_id)
    await ledger.begin(intent)
    await ledger.mark_dispatch_started(intent)
    await ledger.settle(receipt)
    assert ledger.total_usage.input_tokens == 111
    assert ledger.total_cost_usd is None
    second = replace(intent, delivery_attempt_id="priced-second")
    await ledger.begin(second)
    await ledger.mark_dispatch_started(second)
    await ledger.settle(replace(receipt, delivery_attempt_id="priced-second", cost_usd=0.25))
    assert ledger.total_cost_usd == 0.25
    assert ledger.usage_complete is True


async def test_corrupt_snapshot_and_conflicting_journal_fail_closed() -> None:
    from linch.events import ModelCallEvent
    from linch.model_calls import _ModelCallError, _ModelCallLedger, receipt_to_dict

    original = await _settled()
    snapshot = original.snapshot()
    snapshot["total_usage"]["input_tokens"] = 0
    with pytest.raises(_ModelCallError):
        await _ModelCallLedger("run-fixture").restore(snapshot, [])
    _, conflicting = _records(cost_usd=0.5)
    event = ModelCallEvent("run-fixture", "receipt", receipt_to_dict(conflicting))
    with pytest.raises(_ModelCallError):
        await _ModelCallLedger("run-fixture").restore(original.snapshot(), [event])


async def test_model_call_event_codec_is_strict_and_detached() -> None:
    from linch import ModelCallEvent, is_model_call_event
    from linch.events import event_from_dict, event_to_dict
    from linch.model_calls import _ModelCallError, receipt_to_dict

    _, receipt = _records()
    record = receipt_to_dict(receipt)
    event = ModelCallEvent("run-fixture", "receipt", record, budget_owner_id="owner-fixture")
    record["usage"]["input_tokens"] = 999
    assert event.record["usage"]["input_tokens"] == 111
    raw = event_to_dict(event)
    assert raw["type"] == "model_call"
    assert is_model_call_event(event)
    restored = event_from_dict(raw)
    raw["record"]["usage"]["input_tokens"] = 999
    assert restored.record["usage"]["input_tokens"] == 111
    serialized = event_to_dict(event)
    serialized["schema_version"] = 2
    with pytest.raises(_ModelCallError):
        event_from_dict(serialized)
    serialized = event_to_dict(event)
    serialized["run_id"] = "wrong-run"
    with pytest.raises(_ModelCallError):
        event_from_dict(serialized)


def test_usage_event_legacy_wire_shape_stays_unchanged() -> None:
    from linch.events import UsageEvent, event_from_dict, event_to_dict
    from linch.types import Usage

    event = UsageEvent(Usage(111, 22), Usage(111, 22))
    expected = {
        "type": "usage",
        "usage": {
            "input_tokens": 111,
            "output_tokens": 22,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
        },
        "cumulative": {
            "input_tokens": 111,
            "output_tokens": 22,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
        },
    }
    assert event_to_dict(event) == expected
    assert event_to_dict(event_from_dict(expected)) == expected


def test_auxiliary_usage_metadata_and_incomplete_result_roundtrip() -> None:
    from linch.events import ResultEvent, UsageEvent, event_from_dict, event_to_dict
    from linch.types import Usage

    usage = UsageEvent(
        Usage(111, 22),
        Usage(111, 22),
        source="compaction",
        call_id="delivery-fixture",
        logical_call_id="logical-fixture",
        model="gpt-5",
    )
    assert event_to_dict(event_from_dict(event_to_dict(usage))) == event_to_dict(usage)
    result = ResultEvent("error", "error", Usage(111, 22), 17, usage_complete=False)
    assert event_to_dict(result)["usage_complete"] is False
    assert event_from_dict(event_to_dict(result)).usage_complete is False
    assert "usage_complete" not in event_to_dict(replace(result, usage_complete=True))


def test_all_model_call_journal_kinds_roundtrip() -> None:
    from linch.events import ModelCallEvent, event_from_dict, event_to_dict
    from linch.model_calls import intent_to_dict, receipt_to_dict

    intent, receipt = _records()
    records = {
        "intent": intent_to_dict(intent),
        "dispatch_started": intent_to_dict(intent),
        "receipt": receipt_to_dict(receipt),
        "abandoned": {
            "version": 1,
            "intent": intent_to_dict(intent),
            "reason": "cancelled",
            "usage_status": "unknown",
        },
        "projection": {
            "version": 1,
            "projection_id": "projection-fixture",
            "source_receipt_ids": ["delivery-fixture"],
            "strategy_id": "default-v1",
            "source_context_fingerprint": "source-fingerprint-fixture",
            "target_context_fingerprint": "target-fingerprint-fixture",
        },
    }
    for kind, record in records.items():
        event = ModelCallEvent("run-fixture", kind, record)
        assert event_to_dict(event_from_dict(event_to_dict(event))) == event_to_dict(event)


async def test_reported_rejection_is_charged_and_unknown_receipt_is_not_zero() -> None:
    from linch.budget import RunBudget
    from linch.model_calls import _ModelCallError, _ModelCallLedger

    intent, receipt = _records(
        disposition="rejected",
        rejection_reason="empty output",
        accepted_text=None,
        accepted_text_sha256=None,
    )
    budget = RunBudget(max_tokens=1000)
    ledger = _ModelCallLedger(intent.run_id, budget=budget)
    await ledger.begin(intent)
    await ledger.mark_dispatch_started(intent)
    await ledger.settle(receipt)
    assert budget.spent_tokens == 133
    unknown_intent = replace(intent, delivery_attempt_id="unknown-delivery")
    unknown_receipt = replace(
        receipt,
        delivery_attempt_id="unknown-delivery",
        actual_model="",
        usage_status="unknown",
        usage=None,
        cost_usd=None,
        disposition="failed",
        rejection_reason="transport failed",
    )
    await ledger.begin(unknown_intent)
    await ledger.mark_dispatch_started(unknown_intent)
    await ledger.settle(unknown_receipt)
    assert ledger.total_usage.input_tokens == 111
    assert ledger.usage_complete is False
    with pytest.raises(_ModelCallError):
        ledger.assert_may_start()
    fresh = _ModelCallLedger(intent.run_id, budget=RunBudget(max_tokens=1000))
    await fresh.restore(ledger.snapshot(), [])
    assert fresh.usage_complete is False


def test_budget_receipt_deduplication_checks_full_usage_payload() -> None:
    from linch.budget import RunBudget
    from linch.errors import ConfigError
    from linch.types import Usage

    budget = RunBudget()
    assert budget._charge_receipt("delivery-fixture", Usage(111, 22), None) is True
    assert budget._charge_receipt("delivery-fixture", Usage(111, 22), None) is False
    with pytest.raises(ConfigError):
        budget._charge_receipt("delivery-fixture", Usage(22, 111), None)
    assert budget.spent_tokens == 133


async def test_public_charge_remains_compatible_after_receipt_ownership() -> None:
    from linch.budget import RunBudget
    from linch.model_calls import _ModelCallLedger
    from linch.types import Usage

    budget = RunBudget(max_tokens=1000)
    ledger = await _settled(budget=budget)
    budget.charge(Usage(10, 2, 3, 4), 0.1)
    await _ModelCallLedger("run-fixture", budget=budget).restore(ledger.snapshot(), [])
    assert budget.spent_tokens == 152
    assert budget.spent_usd == pytest.approx(0.10035875)


async def test_concurrent_duplicate_settlement_charges_once() -> None:
    import asyncio

    from linch.budget import RunBudget
    from linch.model_calls import _ModelCallLedger

    async def persist(event: Any) -> None:
        await asyncio.sleep(0)

    intent, receipt = _records()
    budget = RunBudget()
    ledger = _ModelCallLedger(intent.run_id, budget=budget, persist=persist)
    await ledger.begin(intent)
    await ledger.mark_dispatch_started(intent)
    assert await asyncio.gather(ledger.settle(receipt), ledger.settle(receipt)) == [True, False]
    assert budget.spent_tokens == 133


async def test_event_only_started_recovery_fences_budget() -> None:
    from linch.budget import RunBudget
    from linch.model_calls import _ModelCallError, _ModelCallLedger

    journal = []

    async def persist(event: Any) -> None:
        journal.append(event)

    intent, _ = _records()
    ledger = _ModelCallLedger(intent.run_id, persist=persist)
    await ledger.begin(intent)
    await ledger.mark_dispatch_started(intent)
    restored = _ModelCallLedger(intent.run_id, budget=RunBudget(max_cost_usd=1.0))
    await restored.restore(None, journal)
    assert restored.usage_complete is False
    with pytest.raises(_ModelCallError):
        restored.assert_may_start()


async def test_dispatch_after_abandonment_in_journal_fails_closed() -> None:
    from linch.events import ModelCallEvent
    from linch.model_calls import _ModelCallError, _ModelCallLedger, intent_to_dict

    intent, _ = _records()
    raw = intent_to_dict(intent)
    journal = [
        ModelCallEvent(intent.run_id, "intent", raw),
        ModelCallEvent(
            intent.run_id,
            "abandoned",
            {
                "version": 1,
                "intent": raw,
                "reason": "cancelled",
                "usage_status": "not_dispatched",
            },
        ),
        ModelCallEvent(intent.run_id, "dispatch_started", raw),
    ]
    with pytest.raises(_ModelCallError):
        await _ModelCallLedger(intent.run_id).restore(None, journal)


async def test_owner_restore_requires_shared_unknown_fence() -> None:
    from linch.budget import RunBudget
    from linch.errors import ConfigError
    from linch.model_calls import _ModelCallLedger

    intent, _ = _records()
    budget = RunBudget(max_tokens=1000)
    ledger = _ModelCallLedger(intent.run_id, budget=budget)
    await ledger.begin(intent)
    await ledger.mark_dispatch_started(intent)
    await ledger.abandon(intent, reason="cancelled")
    budget._unknown_delivery_ids.clear()
    with pytest.raises(ConfigError):
        await _ModelCallLedger(intent.run_id, budget=budget).restore(ledger.snapshot(), [])


@pytest.mark.parametrize("stage", ["intent", "dispatch_started"])
async def test_pre_dispatch_journal_failure_never_invents_spend(stage: str) -> None:
    from linch.budget import RunBudget
    from linch.model_calls import _ModelCallError, _ModelCallLedger

    async def persist(event: Any) -> None:
        if event.record_kind == stage:
            raise OSError("journal unavailable")

    intent, _ = _records()
    budget = RunBudget(max_tokens=1000)
    ledger = _ModelCallLedger(intent.run_id, budget=budget, persist=persist)
    with pytest.raises(_ModelCallError):
        await ledger.begin(intent)
        await ledger.mark_dispatch_started(intent)
    assert budget.spent_tokens == 0
    assert ledger.usage_complete is True
    with pytest.raises(_ModelCallError):
        ledger.assert_may_start()


@pytest.mark.parametrize("value", [None, 1, "false", []])
def test_result_usage_complete_rejects_non_boolean_metadata(value: Any) -> None:
    from linch.events import event_from_dict

    with pytest.raises(ValueError):
        event_from_dict({"type": "result", "usage_complete": value})


@pytest.mark.parametrize("field", ["source", "call_id", "logical_call_id", "model"])
def test_auxiliary_usage_metadata_rejects_non_string_fields(field: str) -> None:
    from linch.events import event_from_dict

    with pytest.raises(ValueError):
        event_from_dict({"type": "usage", field: ["invalid"]})


async def test_abandoned_snapshot_overlapping_full_journal_replays_idempotently() -> None:
    from linch.budget import RunBudget
    from linch.model_calls import _ModelCallError, _ModelCallLedger

    journal = []

    async def persist(event: Any) -> None:
        journal.append(event)

    intent, _ = _records()
    ledger = _ModelCallLedger(intent.run_id, persist=persist)
    await ledger.begin(intent)
    await ledger.mark_dispatch_started(intent)
    await ledger.abandon(intent, reason="cancelled")
    restored = _ModelCallLedger(intent.run_id, budget=RunBudget(max_tokens=1000))
    await restored.restore(ledger.snapshot(), journal)
    assert restored.usage_complete is False
    with pytest.raises(_ModelCallError):
        restored.assert_may_start()


async def test_corrupt_journal_cumulative_cost_is_validated_before_budget_restore() -> None:
    from linch.budget import RunBudget
    from linch.events import ModelCallEvent
    from linch.model_calls import (
        _ModelCallError,
        _ModelCallLedger,
        intent_to_dict,
        receipt_to_dict,
    )

    intent, receipt = _records(cost_usd=1e308)
    journal = []
    for delivery_id in ("huge-cost-one", "huge-cost-two"):
        attempt = replace(intent, delivery_attempt_id=delivery_id)
        raw = intent_to_dict(attempt)
        journal.extend(
            [
                ModelCallEvent(intent.run_id, "intent", raw),
                ModelCallEvent(intent.run_id, "dispatch_started", raw),
                ModelCallEvent(
                    intent.run_id,
                    "receipt",
                    receipt_to_dict(replace(receipt, delivery_attempt_id=delivery_id)),
                ),
            ]
        )
    budget = RunBudget(max_cost_usd=1.0)
    with pytest.raises(_ModelCallError):
        await _ModelCallLedger(intent.run_id, budget=budget).restore(None, journal)
    assert budget.spent_tokens == 0
    assert budget.spent_usd == 0.0


async def test_started_delivery_cannot_settle_as_not_dispatched() -> None:
    from linch.budget import RunBudget
    from linch.model_calls import _ModelCallError, _ModelCallLedger

    intent, receipt = _records(
        actual_model="",
        usage_status="not_dispatched",
        usage=None,
        cost_usd=None,
        disposition="not_dispatched",
        accepted_text=None,
        accepted_text_sha256=None,
    )
    budget = RunBudget(max_tokens=1000)
    ledger = _ModelCallLedger(intent.run_id, budget=budget)
    await ledger.begin(intent)
    await ledger.mark_dispatch_started(intent)
    with pytest.raises(_ModelCallError) as error:
        await ledger.settle(receipt)
    assert error.value.code == "invalid_state"
    assert ledger.snapshot()["settled_delivery_ids"] == []
    assert ledger.snapshot()["started_delivery_ids"] == [intent.delivery_attempt_id]
    await ledger.abandon(intent, reason="dispatched delivery has no reported usage")
    with pytest.raises(_ModelCallError):
        _ModelCallLedger("sibling", budget=budget).assert_may_start()
    assert ledger.usage_complete is False


@pytest.mark.parametrize("source", ["journal", "snapshot", "snapshot_and_journal"])
async def test_recovery_rejects_not_dispatched_receipt_for_started_delivery(source: str) -> None:
    from linch.budget import RunBudget
    from linch.events import ModelCallEvent
    from linch.model_calls import (
        _ModelCallError,
        _ModelCallLedger,
        intent_to_dict,
        receipt_to_dict,
    )

    intent, receipt = _records(
        actual_model="",
        usage_status="not_dispatched",
        usage=None,
        cost_usd=None,
        disposition="not_dispatched",
        accepted_text=None,
        accepted_text_sha256=None,
    )
    raw_intent = intent_to_dict(intent)
    journal = [
        ModelCallEvent(intent.run_id, "intent", raw_intent),
        ModelCallEvent(intent.run_id, "dispatch_started", raw_intent),
        ModelCallEvent(intent.run_id, "receipt", receipt_to_dict(receipt)),
    ]
    state = None
    if source != "journal":
        state = _ModelCallLedger(intent.run_id).snapshot()
        state["receipts"] = [receipt_to_dict(receipt)]
        state["settled_delivery_ids"] = [intent.delivery_attempt_id]
    budget = RunBudget(max_tokens=1000)
    restored = _ModelCallLedger(intent.run_id, budget=budget)
    with pytest.raises(_ModelCallError):
        await restored.restore(state, journal if source != "snapshot" else [])
    assert budget.spent_tokens == 0
    assert budget._budget_owner_id is None


@pytest.mark.parametrize("restore_budget", ["same_owner", "fresh_zero"])
async def test_budgeted_checkpoint_before_first_intent_reconciles_later_journal(
    restore_budget: str,
) -> None:
    from linch.budget import RunBudget
    from linch.model_calls import _ModelCallLedger

    journal = []

    async def persist(event: Any) -> None:
        journal.append(event)

    intent, receipt = _records()
    budget = RunBudget(max_tokens=1000)
    ledger = _ModelCallLedger(intent.run_id, budget=budget, persist=persist)
    checkpoint = ledger.snapshot()
    await ledger.begin(intent)
    await ledger.mark_dispatch_started(intent)
    await ledger.settle(receipt)
    resumed_budget = budget if restore_budget == "same_owner" else RunBudget(max_tokens=1000)
    restored = _ModelCallLedger(intent.run_id, budget=resumed_budget)
    await restored.restore(checkpoint, journal)
    assert restored.total_usage.input_tokens == 111
    assert restored.total_usage.output_tokens == 22
    assert resumed_budget.spent_tokens == 133
    assert restored.snapshot()["budget_owner_id"] == checkpoint["budget_owner_id"]
    assert all(event.budget_owner_id == checkpoint["budget_owner_id"] for event in journal)


async def test_predispatch_shared_budget_snapshots_keep_one_owner() -> None:
    from linch.budget import RunBudget
    from linch.model_calls import _ModelCallLedger

    budget = RunBudget(max_tokens=1000)
    intent, receipt = _records()
    first = _ModelCallLedger(intent.run_id, budget=budget)
    sibling = _ModelCallLedger("sibling", budget=budget)
    first_checkpoint = first.snapshot()
    sibling_checkpoint = sibling.snapshot()
    await first.begin(intent)
    await first.mark_dispatch_started(intent)
    await first.settle(receipt)
    restored_sibling = _ModelCallLedger("sibling", budget=budget)
    await restored_sibling.restore(sibling_checkpoint, [])
    restored_sibling.assert_may_start()
    assert budget.spent_tokens == 133
    assert sibling_checkpoint["budget_owner_id"] == first_checkpoint["budget_owner_id"]
    assert restored_sibling.snapshot()["budget_owner_id"] == first_checkpoint["budget_owner_id"]
