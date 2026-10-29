"""Strict physical-delivery receipts and run-local accounting.

The ledger records observed spending before awaiting journal durability. A failed
write poisons continuation while retaining that observed lower bound in memory.
Delivery IDs, rather than logical operations, are the unit of deduplication.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import uuid4

from .types import Usage

if TYPE_CHECKING:
    from .budget import RunBudget
    from .events import ModelCallEvent

ModelCallKind = Literal["primary", "compaction"]
UsageStatus = Literal["reported", "unknown", "not_dispatched"]
CallDisposition = Literal[
    "accepted", "rejected", "rejected_native", "cancelled", "failed", "not_dispatched"
]

_VERSION = 1
_ACCEPTED_TEXT_MAX_BYTES = 1024 * 1024
_USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")
_RECORD_KINDS = frozenset({"intent", "dispatch_started", "receipt", "abandoned", "projection"})


class _ModelCallError(RuntimeError):
    """An accounting invariant failed; ``code`` never includes provider data."""

    def __init__(self, code: str, message: str) -> None:
        if code not in {
            "invalid_record",
            "invalid_state",
            "identity_mismatch",
            "conflicting_delivery",
            "persistence_failed",
            "budget_accounting_unknown",
            "budget_exceeded",
        }:
            raise ValueError("unknown model-call error code")
        self.code = code
        super().__init__(message)


def _invalid(message: str) -> _ModelCallError:
    return _ModelCallError("invalid_record", message)


def _string(value: Any, name: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise _invalid(f"{name} must be a nonblank string")
    return value


def _optional_string(value: Any, name: str) -> str | None:
    return None if value is None else _string(value, name)


def _integer(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise _invalid(f"{name} must be a nonnegative integer")
    return value


def _cost(value: Any) -> float | None:
    if value is None:
        return None
    if type(value) not in (int, float):
        raise _invalid("cost must be a finite nonnegative number or null")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise _invalid("cost must be finite") from exc
    if not math.isfinite(result) or result < 0:
        raise _invalid("cost must be a finite nonnegative number or null")
    return result


def _copy_usage(usage: Usage) -> Usage:
    if not isinstance(usage, Usage):
        raise _invalid("reported usage must be Usage")
    return Usage(**{name: _integer(getattr(usage, name), name) for name in _USAGE_FIELDS})


def _usage_dict(usage: Usage) -> dict[str, int]:
    owned = _copy_usage(usage)
    return {name: getattr(owned, name) for name in _USAGE_FIELDS}


def _usage_from_dict(raw: Any) -> Usage:
    if not isinstance(raw, Mapping) or set(raw) != set(_USAGE_FIELDS):
        raise _invalid("usage must contain all four token buckets")
    return Usage(**{name: _integer(raw[name], name) for name in _USAGE_FIELDS})


def _versioned(raw: Mapping[str, Any], required: set[str]) -> None:
    if not isinstance(raw, Mapping) or set(raw) != required | {"version"}:
        raise _invalid("model-call record has missing or unknown fields")
    if type(raw["version"]) is not int or raw["version"] != _VERSION:
        raise _invalid("unsupported model-call record version")


@dataclass(frozen=True, slots=True)
class ModelCallIntent:
    logical_call_id: str
    delivery_attempt_id: str
    parent_logical_call_id: str | None
    kind: ModelCallKind
    run_id: str
    turn_index: int | None
    requested_model: str
    request_fingerprint: str
    strategy_id: str | None


@dataclass(frozen=True, slots=True)
class ModelCallReceipt:
    logical_call_id: str
    delivery_attempt_id: str
    kind: ModelCallKind
    run_id: str
    turn_index: int | None
    actual_model: str
    usage_status: UsageStatus
    usage: Usage | None
    cost_usd: float | None
    disposition: CallDisposition
    rejection_reason: str | None
    accepted_text: str | None
    accepted_text_sha256: str | None
    duration_ms: int
    request_fingerprint: str
    strategy_id: str | None
    source_context_fingerprint: str | None

    def __post_init__(self) -> None:
        if self.usage is not None:
            object.__setattr__(self, "usage", _copy_usage(self.usage))


@dataclass(frozen=True, slots=True)
class AuxiliaryTextResult:
    receipt: ModelCallReceipt
    text: str | None


_INTENT_FIELDS = {field.name for field in fields(ModelCallIntent)}
_RECEIPT_FIELDS = {field.name for field in fields(ModelCallReceipt)}
_IDENTITY_FIELDS = (
    "logical_call_id",
    "delivery_attempt_id",
    "kind",
    "run_id",
    "turn_index",
    "request_fingerprint",
    "strategy_id",
)


def _validate_identity(raw: Mapping[str, Any]) -> None:
    for name in ("logical_call_id", "delivery_attempt_id", "run_id", "request_fingerprint"):
        _string(raw[name], name)
    if raw["kind"] not in ("primary", "compaction"):
        raise _invalid("unsupported model-call kind")
    if raw["turn_index"] is not None:
        _integer(raw["turn_index"], "turn_index")
    _optional_string(raw["strategy_id"], "strategy_id")
    if raw["kind"] == "compaction" and raw["strategy_id"] is None:
        raise _invalid("compaction must identify its strategy")


def intent_from_dict(raw: Mapping[str, Any]) -> ModelCallIntent:
    _versioned(raw, _INTENT_FIELDS)
    _validate_identity(raw)
    _string(raw["requested_model"], "requested_model")
    _optional_string(raw["parent_logical_call_id"], "parent_logical_call_id")
    return ModelCallIntent(**{name: raw[name] for name in _INTENT_FIELDS})


def intent_to_dict(intent: ModelCallIntent) -> dict[str, Any]:
    if not isinstance(intent, ModelCallIntent):
        raise _invalid("intent must be ModelCallIntent")
    raw = {"version": _VERSION, **{name: getattr(intent, name) for name in _INTENT_FIELDS}}
    intent_from_dict(raw)
    return raw


def receipt_from_dict(raw: Mapping[str, Any]) -> ModelCallReceipt:
    _versioned(raw, _RECEIPT_FIELDS)
    _validate_identity(raw)
    status = raw["usage_status"]
    if status not in ("reported", "unknown", "not_dispatched"):
        raise _invalid("unsupported usage status")
    disposition = raw["disposition"]
    if disposition not in (
        "accepted",
        "rejected",
        "rejected_native",
        "cancelled",
        "failed",
        "not_dispatched",
    ):
        raise _invalid("unsupported call disposition")
    _string(raw["actual_model"], "actual_model", empty=status != "reported")
    _integer(raw["duration_ms"], "duration_ms")
    _optional_string(raw["rejection_reason"], "rejection_reason")
    _optional_string(raw["source_context_fingerprint"], "source_context_fingerprint")
    cost = _cost(raw["cost_usd"])
    usage = _usage_from_dict(raw["usage"]) if status == "reported" else None
    if status != "reported" and (raw["usage"] is not None or cost is not None):
        raise _invalid("unreported usage cannot have tokens or cost")
    if (status == "not_dispatched") != (disposition == "not_dispatched"):
        raise _invalid("not_dispatched usage and disposition must agree")
    if status == "unknown" and disposition not in ("cancelled", "failed"):
        raise _invalid("unknown usage must describe a cancelled or failed delivery")
    text, digest = raw["accepted_text"], raw["accepted_text_sha256"]
    if raw["kind"] == "compaction" and disposition == "accepted":
        _string(text, "accepted_text")
        try:
            encoded = text.encode("utf-8")
        except UnicodeError as exc:
            raise _invalid("accepted text must be UTF-8") from exc
        if len(encoded) > _ACCEPTED_TEXT_MAX_BYTES:
            raise _invalid("accepted text exceeds the byte bound")
        if digest != hashlib.sha256(encoded).hexdigest():
            raise _invalid("accepted text digest does not match")
    elif text is not None or digest is not None:
        raise _invalid("only accepted compaction receipts may retain text")
    if disposition == "accepted" and (status != "reported" or raw["rejection_reason"] is not None):
        raise _invalid("accepted output requires reported usage and no rejection")
    values = {name: raw[name] for name in _RECEIPT_FIELDS}
    values.update(usage=usage, cost_usd=cost)
    return ModelCallReceipt(**values)


def receipt_to_dict(receipt: ModelCallReceipt) -> dict[str, Any]:
    if not isinstance(receipt, ModelCallReceipt):
        raise _invalid("receipt must be ModelCallReceipt")
    raw = {"version": _VERSION, **{name: getattr(receipt, name) for name in _RECEIPT_FIELDS}}
    raw["usage"] = _usage_dict(receipt.usage) if receipt.usage is not None else None
    receipt_from_dict(raw)
    return raw


def _copy_json_object(raw: Any) -> dict[str, Any]:
    def validate(value: Any, active: set[int], depth: int) -> None:
        if value is None or type(value) in (str, bool, int):
            return
        if type(value) is float and math.isfinite(value):
            return
        if not isinstance(value, list | dict) or depth > 64 or id(value) in active:
            raise _invalid("journal record must be bounded, finite JSON without cycles")
        active.add(id(value))
        try:
            if isinstance(value, dict):
                if any(not isinstance(key, str) for key in value):
                    raise _invalid("journal object keys must be strings")
                children = value.values()
            else:
                children = value
            for child in children:
                validate(child, active, depth + 1)
        finally:
            active.remove(id(value))

    if not isinstance(raw, dict):
        raise _invalid("journal record must be an object")
    validate(raw, set(), 0)
    return deepcopy(raw)


def _copy_model_call_record(
    run_id: Any, record_kind: Any, record: Any, budget_owner_id: Any, schema_version: Any
) -> dict[str, Any]:
    """Validate the public envelope, including its kind-specific private codec."""
    _string(run_id, "run_id")
    _optional_string(budget_owner_id, "budget_owner_id")
    if type(schema_version) is not int or schema_version != _VERSION:
        raise _invalid("unsupported model-call envelope version")
    if not isinstance(record_kind, str) or record_kind not in _RECORD_KINDS:
        raise _invalid("unsupported model-call journal kind")
    owned = _copy_json_object(record)
    if record_kind in ("intent", "dispatch_started"):
        identity = intent_from_dict(owned)
    elif record_kind == "receipt":
        identity = receipt_from_dict(owned)
    elif record_kind == "abandoned":
        _versioned(owned, {"intent", "reason", "usage_status"})
        identity = intent_from_dict(owned["intent"])
        _string(owned["reason"], "reason")
        if len(owned["reason"].encode("utf-8")) > 1024:
            raise _invalid("abandonment reason exceeds the byte bound")
        if owned["usage_status"] not in ("unknown", "not_dispatched"):
            raise _invalid("abandoned delivery has invalid usage status")
    else:
        if type(owned.get("version")) is not int or owned["version"] != _VERSION:
            raise _invalid("unsupported projection record version")
        _string(owned.get("projection_id"), "projection_id")
        _string(owned.get("strategy_id"), "strategy_id")
        _string(owned.get("source_context_fingerprint"), "source_context_fingerprint")
        _string(owned.get("target_context_fingerprint"), "target_context_fingerprint")
        ids = owned.get("source_receipt_ids")
        if (
            not isinstance(ids, list)
            or not ids
            or any(not isinstance(x, str) or not x for x in ids)
        ):
            raise _invalid("projection must identify its source receipts")
        if len(set(ids)) != len(ids):
            raise _invalid("projection source receipts must be unique")
        return owned
    if identity.run_id != run_id:
        raise _ModelCallError("identity_mismatch", "journal run and record run do not match")
    return owned


def _require_dispatched_receipt(receipt: ModelCallReceipt) -> None:
    """Ledger receipts close dispatched deliveries; earlier exits are abandonments."""
    if receipt.usage_status == "not_dispatched":
        raise _ModelCallError(
            "invalid_state", "pre-dispatch exits must be recorded as abandoned intents"
        )


def _match(intent: ModelCallIntent, receipt: ModelCallReceipt) -> None:
    if any(getattr(intent, field) != getattr(receipt, field) for field in _IDENTITY_FIELDS):
        raise _ModelCallError("identity_mismatch", "receipt does not match its dispatched intent")


def _totals(receipts: Sequence[ModelCallReceipt]) -> tuple[Usage, float | None]:
    usage, cost = Usage(), None
    for receipt in receipts:
        if receipt.usage is not None:
            usage = usage.add(receipt.usage)
        if receipt.cost_usd is not None:
            cost = (cost or 0.0) + receipt.cost_usd
            if not math.isfinite(cost):
                raise _invalid("cumulative cost must remain finite")
    return usage, cost


class _ModelCallLedger:
    def __init__(
        self,
        run_id: str,
        *,
        budget: RunBudget | None = None,
        persist: Callable[[ModelCallEvent], Awaitable[None]] | None = None,
    ) -> None:
        self.run_id = _string(run_id, "run_id")
        self._budget = budget
        self._persist = persist
        self._budget_owner_id = (
            budget._budget_owner_id
            if budget is not None and budget._budget_owner_id
            else str(uuid4())
        )
        self._intents: dict[str, ModelCallIntent] = {}
        self._started: set[str] = set()
        self._abandoned: set[str] = set()
        self._receipts: dict[str, ModelCallReceipt] = {}
        self._observed: dict[str, ModelCallReceipt] = {}
        self._consumed_auxiliary_receipt_ids: set[str] = set()
        self._journal: list[ModelCallEvent] = []
        self._total_usage = Usage()
        self._total_cost_usd: float | None = None
        self._has_unknown_usage = False
        self._persistence_failed = False
        self._lock = asyncio.Lock()

    @property
    def total_usage(self) -> Usage:
        return _copy_usage(self._total_usage)

    @property
    def total_cost_usd(self) -> float | None:
        return self._total_cost_usd

    @property
    def usage_complete(self) -> bool:
        return not self._has_unknown_usage

    def _healthy(self) -> None:
        if self._persistence_failed:
            raise _ModelCallError("persistence_failed", "model-call journal durability failed")

    def _bind_budget(self) -> None:
        if self._budget is not None:
            self._budget_owner_id = self._budget._receipt_owner()

    def assert_may_start(self) -> None:
        self._healthy()
        if self._budget is not None:
            if self._budget._is_capped and (
                self._has_unknown_usage or self._budget._unknown_delivery_ids
            ):
                raise _ModelCallError(
                    "budget_accounting_unknown", "budget cannot admit a call after unknown use"
                )
            if self._budget.exceeded:
                raise _ModelCallError("budget_exceeded", "model-call budget is exhausted")

    def _intent(self, intent: ModelCallIntent) -> ModelCallIntent:
        owned = intent_from_dict(intent_to_dict(intent))
        if owned.run_id != self.run_id:
            raise _ModelCallError("identity_mismatch", "intent belongs to another run")
        return owned

    def _pending(self, intent: ModelCallIntent) -> ModelCallIntent:
        owned = self._intent(intent)
        if self._intents.get(owned.delivery_attempt_id) != owned:
            raise _ModelCallError("identity_mismatch", "intent was not admitted by this ledger")
        return owned

    async def _write(self, kind: str, record: dict[str, Any]) -> None:
        from .events import ModelCallEvent

        event = ModelCallEvent(
            self.run_id, cast(Any, kind), record, budget_owner_id=self._budget_owner_id
        )
        if self._persist is not None:
            try:
                await self._persist(
                    ModelCallEvent(
                        event.run_id,
                        event.record_kind,
                        event.record,
                        budget_owner_id=event.budget_owner_id,
                    )
                )
            except BaseException as exc:
                self._persistence_failed = True
                if not isinstance(exc, Exception):
                    raise
                raise _ModelCallError(
                    "persistence_failed", "model-call journal durability failed"
                ) from exc
        self._journal.append(event)

    async def begin(self, intent: ModelCallIntent) -> None:
        async with self._lock:
            self._healthy()
            owned = self._intent(intent)
            delivery_id = owned.delivery_attempt_id
            if delivery_id in self._intents or delivery_id in self._receipts:
                raise _ModelCallError("conflicting_delivery", "delivery intent already exists")
            self._bind_budget()
            await self._write("intent", intent_to_dict(owned))
            self._intents[delivery_id] = owned

    async def mark_dispatch_started(self, intent: ModelCallIntent) -> None:
        async with self._lock:
            self._healthy()
            owned = self._pending(intent)
            delivery_id = owned.delivery_attempt_id
            if delivery_id in self._started or delivery_id in self._abandoned:
                raise _ModelCallError("invalid_state", "delivery dispatch already started or ended")
            await self._write("dispatch_started", intent_to_dict(owned))
            self._started.add(delivery_id)

    async def settle(self, receipt: ModelCallReceipt) -> bool:
        async with self._lock:
            self._healthy()
            owned = receipt_from_dict(receipt_to_dict(receipt))
            _require_dispatched_receipt(owned)
            delivery_id = owned.delivery_attempt_id
            if delivery_id in self._receipts:
                if receipt_to_dict(self._receipts[delivery_id]) != receipt_to_dict(owned):
                    raise _ModelCallError(
                        "conflicting_delivery", "duplicate receipt payload differs"
                    )
                return False
            intent = self._intents.get(delivery_id)
            if intent is None or delivery_id not in self._started or delivery_id in self._abandoned:
                raise _ModelCallError(
                    "invalid_state", "receipt requires its matching started intent"
                )
            _match(intent, owned)
            next_usage = self._total_usage.add(owned.usage) if owned.usage else self.total_usage
            next_cost = self._total_cost_usd
            if owned.cost_usd is not None:
                next_cost = _cost((next_cost or 0.0) + owned.cost_usd)
            if self._budget is not None:
                if owned.usage is not None:
                    self._budget._charge_receipt(delivery_id, owned.usage, owned.cost_usd)
                elif owned.usage_status == "unknown":
                    self._budget._register_unresolved_delivery(delivery_id)
            self._observed[delivery_id] = owned
            self._total_usage, self._total_cost_usd = next_usage, next_cost
            self._has_unknown_usage |= owned.usage_status == "unknown"
            await self._write("receipt", receipt_to_dict(owned))
            self._receipts[delivery_id] = owned
            del self._intents[delivery_id]
            self._started.remove(delivery_id)
            return True

    async def abandon(self, intent: ModelCallIntent, *, reason: str) -> None:
        async with self._lock:
            self._healthy()
            owned = self._pending(intent)
            delivery_id = owned.delivery_attempt_id
            if delivery_id in self._abandoned:
                raise _ModelCallError("invalid_state", "delivery already abandoned")
            status = "unknown" if delivery_id in self._started else "not_dispatched"
            record = {
                "version": _VERSION,
                "intent": intent_to_dict(owned),
                "reason": reason,
                "usage_status": status,
            }
            _copy_model_call_record(self.run_id, "abandoned", record, self._budget_owner_id, 1)
            if status == "unknown":
                self._has_unknown_usage = True
                if self._budget is not None:
                    self._budget._register_unresolved_delivery(delivery_id)
            await self._write("abandoned", record)
            self._abandoned.add(delivery_id)

    def snapshot(self) -> dict[str, Any]:
        # Expose one shared owner from the first checkpoint onward. Delaying
        # binding until begin/snapshot keeps a newly constructed resume ledger's
        # fresh budget eligible to adopt the checkpoint owner in restore().
        self._bind_budget()
        result = {
            "version": _VERSION,
            "budget_owner_id": self._budget_owner_id,
            "settled_delivery_ids": sorted(self._receipts),
            "receipts": [receipt_to_dict(value) for value in self._receipts.values()],
            "total_usage": _usage_dict(self._total_usage),
            "known_cost_usd": self._total_cost_usd,
            "has_unknown_usage": self._has_unknown_usage,
            "pending_intents": [intent_to_dict(value) for value in self._intents.values()],
            "consumed_auxiliary_receipt_ids": sorted(self._consumed_auxiliary_receipt_ids),
            "started_delivery_ids": sorted(self._started),
            "abandoned_delivery_ids": sorted(self._abandoned),
        }
        if self._persistence_failed:
            result["persistence_failed"] = True
        return result

    async def restore(
        self,
        state: Mapping[str, Any] | None,
        journal: Sequence[ModelCallEvent],
    ) -> None:
        async with self._lock:
            self._healthy()
            recovery = _LedgerRecovery(self.run_id, self._budget_owner_id)
            if state is not None:
                recovery.load_snapshot(state)
            for event in journal:
                recovery.apply(event)
            unknown = {
                receipt.delivery_attempt_id
                for receipt in recovery.receipts.values()
                if receipt.usage_status == "unknown"
            } | (recovery.started - recovery.receipts.keys())
            total_usage, total_cost = _totals(list(recovery.receipts.values()))
            if self._budget is not None:
                mode = self._budget._restore_receipts(
                    recovery.owner_id, list(recovery.receipts.values())
                )
                self._budget._restore_unresolved_deliveries(unknown, fresh=mode == "fresh_adopted")
            self._budget_owner_id = recovery.owner_id
            self._receipts = recovery.receipts
            self._observed = dict(recovery.receipts)
            self._intents = {
                key: value for key, value in recovery.intents.items() if key not in self._receipts
            }
            self._started = recovery.started - self._receipts.keys()
            self._abandoned = recovery.abandoned
            self._consumed_auxiliary_receipt_ids = recovery.consumed
            self._has_unknown_usage = bool(unknown)
            self._total_usage, self._total_cost_usd = total_usage, total_cost
            self._journal = list(recovery.journal)


class _LedgerRecovery:
    """Validate a recovery plan completely before changing a live budget."""

    def __init__(self, run_id: str, owner_id: str) -> None:
        self.run_id, self.owner_id = run_id, owner_id
        self.owner_bound = False
        self.receipts: dict[str, ModelCallReceipt] = {}
        self.intents: dict[str, ModelCallIntent] = {}
        self.started: set[str] = set()
        self.abandoned: set[str] = set()
        self.consumed: set[str] = set()
        self.journal: list[ModelCallEvent] = []
        self.abandoned_records: dict[str, dict[str, Any]] = {}

    def _owner(self, owner: str | None) -> None:
        if owner is None:
            return
        _string(owner, "budget_owner_id")
        if self.owner_bound and owner != self.owner_id:
            raise _ModelCallError("identity_mismatch", "journal budget owner does not match")
        self.owner_id, self.owner_bound = owner, True

    def _run(self, run_id: str) -> None:
        if run_id != self.run_id:
            raise _ModelCallError("identity_mismatch", "accounting state belongs to another run")

    def _ids(self, value: Any) -> set[str]:
        if not isinstance(value, list):
            raise _invalid("delivery IDs must be a list")
        for item in value:
            _string(item, "delivery_attempt_id")
        if len(set(value)) != len(value):
            raise _invalid("duplicate delivery IDs in accounting state")
        return set(value)

    def _list(self, value: Any) -> list[Any]:
        if not isinstance(value, list):
            raise _invalid("accounting records must be a list")
        return value

    def load_snapshot(self, state: Mapping[str, Any]) -> None:
        required = {
            "budget_owner_id",
            "settled_delivery_ids",
            "receipts",
            "total_usage",
            "known_cost_usd",
            "has_unknown_usage",
            "pending_intents",
            "consumed_auxiliary_receipt_ids",
        }
        optional = {"started_delivery_ids", "abandoned_delivery_ids", "persistence_failed"}
        if not isinstance(state, Mapping) or not required <= state.keys():
            raise _invalid("accounting snapshot is missing required fields")
        if set(state) - required - optional - {"version"}:
            raise _invalid("accounting snapshot has unknown fields")
        if type(state.get("version")) is not int or state["version"] != _VERSION:
            raise _invalid("unsupported accounting snapshot version")
        if state.get("persistence_failed", False) is not False:
            raise _ModelCallError("persistence_failed", "cannot resume failed journal durability")
        self._owner(_string(state["budget_owner_id"], "budget_owner_id"))
        for raw in self._list(state["receipts"]):
            receipt = receipt_from_dict(raw)
            _require_dispatched_receipt(receipt)
            self._run(receipt.run_id)
            if receipt.delivery_attempt_id in self.receipts:
                raise _invalid("duplicate receipt in accounting snapshot")
            self.receipts[receipt.delivery_attempt_id] = receipt
        if self._ids(state["settled_delivery_ids"]) != self.receipts.keys():
            raise _invalid("settled IDs do not match snapshot receipts")
        for raw in self._list(state["pending_intents"]):
            intent = intent_from_dict(raw)
            self._run(intent.run_id)
            delivery_id = intent.delivery_attempt_id
            if delivery_id in self.intents or delivery_id in self.receipts:
                raise _invalid("duplicate pending delivery in accounting snapshot")
            self.intents[delivery_id] = intent
        self.started = self._ids(state.get("started_delivery_ids", []))
        self.abandoned = self._ids(state.get("abandoned_delivery_ids", []))
        if not self.started <= self.intents.keys() or not self.abandoned <= self.intents.keys():
            raise _invalid("dispatch or abandonment marker lacks its pending intent")
        self.consumed = self._ids(state["consumed_auxiliary_receipt_ids"])
        if not self.consumed <= self.receipts.keys() or any(
            self.receipts[key].kind != "compaction" for key in self.consumed
        ):
            raise _invalid("consumed auxiliary IDs must reference compaction receipts")
        usage, cost = _totals(list(self.receipts.values()))
        if usage != _usage_from_dict(state["total_usage"]) or cost != _cost(
            state["known_cost_usd"]
        ):
            raise _invalid("snapshot totals disagree with its receipts")
        unknown = any(value.usage_status == "unknown" for value in self.receipts.values())
        unknown |= bool(self.abandoned & self.started)
        flag = state["has_unknown_usage"]
        if (
            type(flag) is not bool
            or (unknown and not flag)
            or (flag and not unknown and not self.started)
        ):
            raise _invalid("snapshot unknown-usage flag disagrees with its records")

    def apply(self, event: ModelCallEvent) -> None:
        from .events import ModelCallEvent

        if not isinstance(event, ModelCallEvent):
            raise _invalid("accounting journal must contain ModelCallEvent records")
        event = ModelCallEvent(
            event.run_id,
            event.record_kind,
            event.record,
            budget_owner_id=event.budget_owner_id,
            schema_version=event.schema_version,
        )
        self._run(event.run_id)
        self._owner(event.budget_owner_id)
        raw, kind = event.record, event.record_kind
        if kind == "receipt":
            receipt = receipt_from_dict(raw)
            _require_dispatched_receipt(receipt)
            delivery_id = receipt.delivery_attempt_id
            previous = self.receipts.get(delivery_id)
            if previous is not None:
                if receipt_to_dict(previous) != raw:
                    raise _ModelCallError("conflicting_delivery", "journal receipt payload differs")
            else:
                if delivery_id not in self.started or delivery_id in self.abandoned:
                    raise _ModelCallError("invalid_state", "journal receipt lacks a started intent")
                _match(self.intents[delivery_id], receipt)
                self.receipts[delivery_id] = receipt
        elif kind in ("intent", "dispatch_started", "abandoned"):
            intent = intent_from_dict(raw["intent"] if kind == "abandoned" else raw)
            delivery_id = intent.delivery_attempt_id
            previous_intent = self.intents.get(delivery_id)
            if previous_intent is not None and previous_intent != intent:
                raise _ModelCallError("conflicting_delivery", "journal intent payload differs")
            if delivery_id in self.receipts:
                _match(intent, self.receipts[delivery_id])
            if kind == "intent":
                self.intents[delivery_id] = intent
            else:
                if previous_intent is None:
                    raise _ModelCallError("invalid_state", "journal marker lacks its intent")
                if kind == "dispatch_started":
                    if delivery_id in self.abandoned_records:
                        raise _ModelCallError("invalid_state", "abandoned delivery cannot dispatch")
                    self.started.add(delivery_id)
                else:
                    if delivery_id in self.receipts:
                        raise _ModelCallError(
                            "invalid_state", "settled delivery cannot be abandoned"
                        )
                    status = "unknown" if delivery_id in self.started else "not_dispatched"
                    if raw["usage_status"] != status:
                        raise _invalid("abandonment status disagrees with dispatch state")
                    if (
                        delivery_id in self.abandoned_records
                        and self.abandoned_records[delivery_id] != raw
                    ):
                        raise _ModelCallError("conflicting_delivery", "abandonment payload differs")
                    self.abandoned.add(delivery_id)
                    self.abandoned_records[delivery_id] = raw
        else:
            if not set(raw["source_receipt_ids"]) <= self.receipts.keys():
                raise _invalid("projection references missing receipts")
        self.journal.append(event)
