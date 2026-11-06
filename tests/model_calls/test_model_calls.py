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
    assert encoded["version"] == 3
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
    from linch._wire import UnsupportedWireVersionError
    from linch.model_calls import (
        intent_from_dict,
        intent_to_dict,
        receipt_from_dict,
        receipt_to_dict,
    )

    intent, receipt = _records()
    raw = intent_to_dict(intent) if record_kind == "intent" else receipt_to_dict(receipt)
    raw["version"] = 2
    with pytest.raises(UnsupportedWireVersionError):
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
    assert ledger.receipts()[0].usage.output_tokens == 22


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
    assert ledger.receipts() == []
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


@pytest.mark.parametrize("phase", ["intent", "started_tokens", "started_cost", "abandoned"])
async def test_unknown_recovery_fences_budget(phase: str) -> None:
    from linch.budget import RunBudget
    from linch.model_calls import _ModelCallError, _ModelCallLedger

    intent, _ = _records()
    journal = []

    async def persist(event: Any) -> None:
        journal.append(event)

    ledger = _ModelCallLedger(intent.run_id, persist=persist)
    await ledger.begin(intent)
    if phase != "intent":
        await ledger.mark_dispatch_started(intent)
    if phase == "abandoned":
        await ledger.abandon(intent, reason="cancelled")
    budget = RunBudget(max_cost_usd=1.0) if phase == "started_cost" else RunBudget(max_tokens=1000)
    restored = _ModelCallLedger(intent.run_id, budget=budget)
    await restored.restore(journal)
    if phase == "intent":
        restored.assert_may_start()
        assert restored.usage_complete is True
        await restored.abandon(intent, reason="pre-dispatch cancellation")
        restored.assert_may_start()
        assert restored.usage_complete is True
        return
    assert restored.usage_complete is False
    with pytest.raises(_ModelCallError):
        restored.assert_may_start()


@pytest.mark.parametrize("restore_budget", ["same_owner", "fresh_zero"])
async def test_complete_journal_reconciles_by_delivery_without_repricing(
    restore_budget: str,
) -> None:
    from linch.budget import RunBudget
    from linch.model_calls import _ModelCallLedger

    journal = []

    async def persist(event: Any) -> None:
        journal.append(event)

    intent, receipt = _records()
    original = _ModelCallLedger(intent.run_id, persist=persist)
    await original.begin(intent)
    await original.mark_dispatch_started(intent)
    await original.settle(receipt)
    original_owner = journal[0].budget_owner_id
    assert original_owner is not None
    budget = RunBudget(max_tokens=1000)
    restored_budget = budget if restore_budget == "same_owner" else RunBudget(max_tokens=1000)
    restored = _ModelCallLedger(intent.run_id, budget=restored_budget)
    await restored.restore(journal)
    assert restored.total_usage.input_tokens == 111
    assert restored.total_usage.output_tokens == 22
    assert restored.total_cost_usd == 0.00035875
    assert restored.usage_complete is True
    assert restored_budget.spent_tokens == 133
    await restored.restore(journal)
    assert restored_budget.spent_tokens == 133
    assert all(event.budget_owner_id == original_owner for event in journal)


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


async def test_conflicting_journal_receipt_fails_closed() -> None:
    from linch.events import ModelCallEvent
    from linch.model_calls import _ModelCallError, _ModelCallLedger, receipt_to_dict

    journal = []

    async def persist(event: Any) -> None:
        journal.append(event)

    await _settled(persist=persist)
    _, conflicting = _records(cost_usd=0.5)
    event = ModelCallEvent("run-fixture", "receipt", receipt_to_dict(conflicting))
    with pytest.raises(_ModelCallError):
        await _ModelCallLedger("run-fixture").restore([*journal, event])


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
    from linch._wire import UnsupportedWireVersionError

    with pytest.raises(UnsupportedWireVersionError):
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
    from linch.model_calls import _projection_record, intent_to_dict, receipt_to_dict

    intent, receipt = _records()
    records = {
        "intent": intent_to_dict(intent),
        "dispatch_started": intent_to_dict(intent),
        "receipt": receipt_to_dict(receipt),
        "abandoned": {
            "version": 3,
            "intent": intent_to_dict(intent),
            "reason": "cancelled",
            "usage_status": "unknown",
        },
        "projection": _projection_record(
            projection_kind="compaction",
            source_receipt_ids=["delivery-fixture"],
            strategy_id="default-v1",
            strategy_config={
                "implementation": "default_v1",
                "keep_recent_turns": 10,
                "max_output_tokens": 4096,
                "prompt_sha256": "a" * 64,
            },
            source_context_fingerprint="b" * 64,
            target_context_fingerprint="c" * 64,
            full_history_base_count=1,
            full_history_base_fingerprint="d" * 64,
            source_message_fingerprints=["e" * 64],
            reminder_fingerprints=[],
        ),
    }
    for kind, record in records.items():
        event = ModelCallEvent("run-fixture", kind, record)
        assert event_to_dict(event_from_dict(event_to_dict(event))) == event_to_dict(event)


async def test_reported_rejection_is_charged_and_unknown_receipt_is_not_zero() -> None:
    from linch.budget import RunBudget
    from linch.model_calls import _ModelCallError, _ModelCallLedger

    journal = []

    async def persist(event: Any) -> None:
        journal.append(event)

    intent, receipt = _records(
        disposition="rejected",
        rejection_reason="empty output",
        accepted_text=None,
        accepted_text_sha256=None,
    )
    budget = RunBudget(max_tokens=1000)
    ledger = _ModelCallLedger(intent.run_id, budget=budget, persist=persist)
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
    await fresh.restore(journal)
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
    journal = []

    async def persist(event: Any) -> None:
        journal.append(event)

    await _settled(budget=budget, persist=persist)
    budget.charge(Usage(10, 2, 3, 4), 0.1)
    await _ModelCallLedger("run-fixture", budget=budget).restore(journal)
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
                "version": 3,
                "intent": raw,
                "reason": "cancelled",
                "usage_status": "not_dispatched",
            },
        ),
        ModelCallEvent(intent.run_id, "dispatch_started", raw),
    ]
    with pytest.raises(_ModelCallError):
        await _ModelCallLedger(intent.run_id).restore(journal)


async def test_owner_restore_requires_shared_unknown_fence() -> None:
    from linch.budget import RunBudget
    from linch.errors import ConfigError
    from linch.model_calls import _ModelCallLedger

    intent, _ = _records()
    budget = RunBudget(max_tokens=1000)
    journal = []

    async def persist(event: Any) -> None:
        journal.append(event)

    ledger = _ModelCallLedger(intent.run_id, budget=budget, persist=persist)
    await ledger.begin(intent)
    await ledger.mark_dispatch_started(intent)
    await ledger.abandon(intent, reason="cancelled")
    budget._unknown_delivery_ids.clear()
    with pytest.raises(ConfigError):
        await _ModelCallLedger(intent.run_id, budget=budget).restore(journal)


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
        await _ModelCallLedger(intent.run_id, budget=budget).restore(journal)
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
    assert ledger.receipts() == []
    assert ledger.intent_for(intent.delivery_attempt_id) == intent
    await ledger.abandon(intent, reason="dispatched delivery has no reported usage")
    with pytest.raises(_ModelCallError):
        _ModelCallLedger("sibling", budget=budget).assert_may_start()
    assert ledger.usage_complete is False


async def test_recovery_rejects_not_dispatched_receipt_for_started_delivery() -> None:
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
    budget = RunBudget(max_tokens=1000)
    restored = _ModelCallLedger(intent.run_id, budget=budget)
    with pytest.raises(_ModelCallError):
        await restored.restore(journal)
    assert budget.spent_tokens == 0
    assert budget._budget_owner_id is None


def _source_projection_for(intent: Any) -> dict[str, Any]:
    from linch.model_calls import _projection_record

    return _projection_record(
        projection_kind="compaction_source",
        run_id=intent.run_id,
        logical_call_id=intent.logical_call_id,
        delivery_attempt_id=intent.delivery_attempt_id,
        turn_index=intent.turn_index,
        request_fingerprint=intent.request_fingerprint,
        strategy_id=intent.strategy_id,
        strategy_config={
            "implementation": "default_v1",
            "keep_recent_turns": 10,
            "max_output_tokens": 4096,
            "prompt_sha256": "a" * 64,
        },
        source_context_fingerprint="b" * 64,
        full_history_base_count=1,
        full_history_base_fingerprint="c" * 64,
        source_message_fingerprints=["d" * 64],
        reminder_fingerprints=[],
    )


def test_journal_preserves_golden_bytes() -> None:
    import hashlib
    import json

    from linch.model_calls import _copy_json_object, intent_to_dict, receipt_to_dict

    intent, receipt = _records()
    owned = _copy_json_object(
        {"intent": intent_to_dict(intent), "receipt": receipt_to_dict(receipt)}
    )
    encoded = json.dumps(owned, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(encoded.encode()).hexdigest() == (
        "aae1c0d602774a14b851bf76ef076cc1c38b3b1e3030fea12132b8de612a83c0"
    )


@pytest.mark.parametrize("phase", ["started", "settled", "abandoned", "not_dispatched"])
@pytest.mark.parametrize("repeat_intent", [False, True])
async def test_restore_rejects_first_source_after_delivery_phase(
    phase: str,
    repeat_intent: bool,
) -> None:
    """A replayed intent never resets a dispatched or closed delivery's source boundary."""
    from linch.budget import RunBudget
    from linch.events import ModelCallEvent
    from linch.model_calls import _ModelCallError, _ModelCallLedger, intent_to_dict

    intent, receipt = _records(source_context_fingerprint="b" * 64)
    journal = []

    async def persist(event: Any) -> None:
        journal.append(event)

    original = _ModelCallLedger(intent.run_id, persist=persist)
    await original.begin(intent)
    if phase != "not_dispatched":
        await original.mark_dispatch_started(intent)
    if phase == "settled":
        await original.settle(receipt)
    elif phase in {"abandoned", "not_dispatched"}:
        await original.abandon(intent, reason="interrupted")
    if repeat_intent:
        journal.append(ModelCallEvent(intent.run_id, "intent", intent_to_dict(intent)))
    journal.append(ModelCallEvent(intent.run_id, "projection", _source_projection_for(intent)))
    budget = RunBudget(max_tokens=1000)
    restored = _ModelCallLedger(intent.run_id, budget=budget)
    with pytest.raises(_ModelCallError, match="source.*before dispatch|source.*delivery phase"):
        await restored.restore(journal)
    assert budget.spent_tokens == 0
    assert restored.total_usage.input_tokens == restored.total_usage.output_tokens == 0


@pytest.mark.parametrize(
    ("phase", "budget_mode"),
    [
        ("source", "fresh"),
        ("source", "same"),
        ("started", "fresh"),
        ("settled", "fresh"),
        ("settled", "same"),
        ("abandoned", "fresh"),
        ("abandoned", "same"),
    ],
)
async def test_restore_preserves_proven_source_order_against_later_checkpoint(
    phase: str,
    budget_mode: str,
) -> None:
    """Full journal order outranks aggregate phase while exact source duplicates stay harmless."""
    from linch.budget import RunBudget
    from linch.events import ModelCallEvent
    from linch.model_calls import _ModelCallLedger, intent_to_dict

    intent, receipt = _records(source_context_fingerprint="b" * 64)
    journal = []

    async def persist(event: Any) -> None:
        journal.append(event)

    original_budget = RunBudget(max_tokens=1000)
    original = _ModelCallLedger(intent.run_id, budget=original_budget, persist=persist)
    await original.begin(intent)
    source = _source_projection_for(intent)
    await original.record_projection(source)
    if phase != "source":
        await original.mark_dispatch_started(intent)
    if phase == "settled":
        await original.settle(receipt)
    elif phase == "abandoned":
        await original.abandon(intent, reason="interrupted")
    journal.extend(
        [
            ModelCallEvent(intent.run_id, "intent", intent_to_dict(intent)),
            ModelCallEvent(intent.run_id, "projection", source),
        ]
    )
    budget = original_budget if budget_mode == "same" else RunBudget(max_tokens=1000)
    restored = _ModelCallLedger(intent.run_id, budget=budget)
    await restored.restore(journal)
    assert restored.projections("compaction_source") == [source]
    assert budget.spent_tokens == (133 if phase == "settled" else 0)
    assert restored.total_usage.input_tokens == (111 if phase == "settled" else 0)
    await restored.restore(journal)
    assert budget.spent_tokens == (133 if phase == "settled" else 0)


async def test_ledger_events_equal_publicly_validated_events_and_receipts_detach() -> None:
    from linch.events import ModelCallEvent

    written: list[Any] = []

    async def persist(event: Any) -> None:
        written.append(event)

    ledger = await _settled(persist=persist)
    assert [event.record_kind for event in written] == ["intent", "dispatch_started", "receipt"]
    for event in written:
        rebuilt = ModelCallEvent(
            event.run_id, event.record_kind, event.record, budget_owner_id=event.budget_owner_id
        )
        assert rebuilt == event
    returned = ledger.receipts()[0]
    returned.usage.input_tokens = 0
    assert ledger.receipts()[0].usage.input_tokens == 111
    assert ledger.total_usage.input_tokens == 111


async def test_projection_event_record_is_detached_from_the_ledger() -> None:
    from linch.model_calls import _ModelCallLedger

    intent, _receipt = _records(source_context_fingerprint="b" * 64)
    written: list[Any] = []

    async def persist(event: Any) -> None:
        written.append(event)

    ledger = _ModelCallLedger(intent.run_id, persist=persist)
    await ledger.begin(intent)
    source = _source_projection_for(intent)
    await ledger.record_projection(source)
    assert written[-1].record_kind == "projection"
    written[-1].record["projection_id"] = "changed-by-a-listener"
    assert ledger.projections("compaction_source") == [source]
