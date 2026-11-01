"""Run budgets — token/USD spending caps shared across an agent tree.

A :class:`RunBudget` is a plain mutable accumulator.  The run loop charges it
after every provider turn and checks :attr:`RunBudget.exceeded` before each
provider call; subagent child runs inherit the *same object* from their parent
session, so one budget caps the whole tree (parent + all workers).

Token counts sum all four normalized :class:`~linch.types.Usage` buckets (where
input excludes cache reads and writes), matching ``Usage.add`` semantics.  USD
cost comes from :func:`linch.pricing.cost_usd`; unknown models cost ``None`` and
charge ``0.0`` — for unpriced models only the token limit binds.

The event loop is single-threaded, so plain counters are safe — no locks.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from .errors import ConfigError

if TYPE_CHECKING:
    from .model_calls import ModelCallReceipt
    from .types import Usage


@dataclass(slots=True)
class RunBudget:
    """Spending cap for a run (and its subagent tree).

    Args:
        max_tokens: Maximum total tokens that may be charged, or ``None`` for
            no token limit.
        max_cost_usd: Maximum accumulated USD cost, or ``None`` for no cost
            limit.
        warn_ratio: Fraction of either configured limit that triggers the
            one-time warning.
        spent_tokens: Tokens already charged when constructing the budget.
        spent_usd: USD already charged when constructing the budget.
        _warn_emitted: Whether the warning has already been emitted. This
            internal field is exposed by the generated dataclass constructor.

    Attributes:
        max_tokens: Cap on total tokens (all four normalized ``Usage`` buckets summed),
            or ``None`` for no token limit.
        max_cost_usd: Cap on accumulated USD cost, or ``None`` for no limit.
        warn_ratio: Fraction of either limit at which :meth:`take_warning`
            fires (once per budget object).
        spent_tokens: Tokens charged so far.
        spent_usd: USD charged so far (unknown-model turns charge 0).
    """

    max_tokens: int | None = None
    max_cost_usd: float | None = None
    warn_ratio: float = 0.9
    spent_tokens: int = 0
    spent_usd: float = 0.0
    _warn_emitted: bool = field(default=False, repr=False)

    _budget_owner_id: str | None = field(default=None, init=False, repr=False, compare=False)
    _charged_delivery_ids: set[str] = field(
        default_factory=set, init=False, repr=False, compare=False
    )
    _receipt_charges: dict[str, tuple[Usage, float | None]] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    _unknown_delivery_ids: set[str] = field(
        default_factory=set, init=False, repr=False, compare=False
    )
    _expected_spent_tokens: int = field(default=0, init=False, repr=False, compare=False)
    _expected_spent_usd: float = field(default=0.0, init=False, repr=False, compare=False)

    def charge(self, usage: Usage, cost_usd: float | None) -> None:
        """Record one turn's spending.

        Normalized usage contributes all four non-overlapping token buckets.
        Unknown-model usage with ``cost_usd=None`` still contributes tokens but
        contributes zero dollars.

        Args:
            usage: Token usage to add to the budget.
            cost_usd: Resolved USD cost, or ``None`` when the model is unknown.
        """
        self.spent_tokens += (
            usage.input_tokens
            + usage.output_tokens
            + usage.cache_read_tokens
            + usage.cache_creation_tokens
        )
        if cost_usd is not None:
            self.spent_usd += cost_usd
        if self._budget_owner_id is not None:
            self._expected_spent_tokens += (
                usage.input_tokens
                + usage.output_tokens
                + usage.cache_read_tokens
                + usage.cache_creation_tokens
            )
            self._expected_spent_usd += cost_usd or 0.0

    @property
    def _is_capped(self) -> bool:
        return self.max_tokens is not None or self.max_cost_usd is not None

    def _receipt_owner(self) -> str:
        if self._budget_owner_id is None:
            self._budget_owner_id = str(uuid4())
            self._expected_spent_tokens = self.spent_tokens
            self._expected_spent_usd = self.spent_usd
        return self._budget_owner_id

    def _check_receipt_counters(self) -> None:
        if (
            self.spent_tokens != self._expected_spent_tokens
            or self.spent_usd != self._expected_spent_usd
            or self._charged_delivery_ids != self._receipt_charges.keys()
        ):
            raise ConfigError("receipt budget counters or delivery IDs are inconsistent")

    def _charge_receipt(
        self,
        delivery_attempt_id: str,
        usage: Usage,
        cost_usd: float | None,
    ) -> bool:
        from .model_calls import _copy_usage, _cost, _string

        _string(delivery_attempt_id, "delivery_attempt_id")
        owned, cost = _copy_usage(usage), _cost(cost_usd)
        self._receipt_owner()
        self._check_receipt_counters()
        if delivery_attempt_id in self._charged_delivery_ids:
            if self._receipt_charges[delivery_attempt_id] != (owned, cost):
                raise ConfigError("duplicate budget receipt has conflicting spending")
            return False
        _cost(self.spent_usd + (cost or 0.0))
        self.charge(owned, cost)
        self._charged_delivery_ids.add(delivery_attempt_id)
        self._receipt_charges[delivery_attempt_id] = (owned, cost)
        return True

    def _register_unresolved_delivery(self, delivery_attempt_id: str) -> None:
        self._receipt_owner()
        self._unknown_delivery_ids.add(delivery_attempt_id)

    def _restore_receipts(
        self,
        budget_owner_id: str,
        receipts: Sequence[ModelCallReceipt],
    ) -> Literal["same_owner", "fresh_adopted"]:
        from .model_calls import _string, receipt_from_dict, receipt_to_dict

        _string(budget_owner_id, "budget_owner_id")
        owned = [receipt_from_dict(receipt_to_dict(receipt)) for receipt in receipts]
        if len({receipt.delivery_attempt_id for receipt in owned}) != len(owned):
            raise ConfigError("duplicate delivery IDs in restored budget receipts")
        fresh = (
            self._budget_owner_id is None
            and self.spent_tokens == 0
            and self.spent_usd == 0
            and not self._charged_delivery_ids
            and not self._receipt_charges
            and not self._unknown_delivery_ids
        )
        if self._budget_owner_id != budget_owner_id and not fresh:
            raise ConfigError(
                "receipt budget owner mismatch; reuse the owner budget "
                "or supply a fresh zero budget"
            )
        if not fresh:
            self._check_receipt_counters()
            for receipt in owned:
                if receipt.usage is not None:
                    charge = (receipt.usage, receipt.cost_usd)
                    if (
                        receipt.delivery_attempt_id not in self._charged_delivery_ids
                        or self._receipt_charges.get(receipt.delivery_attempt_id) != charge
                    ):
                        raise ConfigError("owner budget is missing a restored receipt charge")
                elif (
                    receipt.usage_status == "unknown"
                    and receipt.delivery_attempt_id not in self._unknown_delivery_ids
                ):
                    raise ConfigError("owner budget is missing its unresolved-delivery fence")
            return "same_owner"
        self._budget_owner_id = budget_owner_id
        self._expected_spent_tokens = self.spent_tokens
        self._expected_spent_usd = self.spent_usd
        for receipt in owned:
            if receipt.usage is not None:
                self._charge_receipt(receipt.delivery_attempt_id, receipt.usage, receipt.cost_usd)
            elif receipt.usage_status == "unknown":
                self._register_unresolved_delivery(receipt.delivery_attempt_id)
        return "fresh_adopted"

    def _restore_unresolved_deliveries(self, delivery_ids: set[str], *, fresh: bool) -> None:
        if fresh:
            self._unknown_delivery_ids.update(delivery_ids)
        elif not delivery_ids <= self._unknown_delivery_ids:
            raise ConfigError("owner budget is missing its unresolved-delivery fence")

    @property
    def remaining_tokens(self) -> int | None:
        """Return the remaining token allowance, or ``None`` when no limit exists.

        Returns:
            Remaining tokens before the configured token limit is reached, or
            ``None`` if no token limit is configured.
        """
        if self.max_tokens is None:
            return None
        return max(0, self.max_tokens - self.spent_tokens)

    @property
    def remaining_usd(self) -> float | None:
        """Return the remaining USD allowance, or ``None`` when no limit exists.

        Returns:
            Remaining USD before the configured cost limit is reached, or
            ``None`` if no cost limit is configured.
        """
        if self.max_cost_usd is None:
            return None
        return max(0.0, self.max_cost_usd - self.spent_usd)

    @property
    def exceeded(self) -> bool:
        """Whether any configured spending limit has been reached.

        Returns:
            ``True`` when token or USD spending is at or above its configured
            limit; otherwise ``False``.
        """
        if self.max_tokens is not None and self.spent_tokens >= self.max_tokens:
            return True
        if self.max_cost_usd is not None and self.spent_usd >= self.max_cost_usd:
            return True
        return False

    def take_warning(self) -> bool:
        """Return ``True`` exactly once, when spending first reaches
        ``warn_ratio`` of any configured limit.

        Shared budgets warn once across the whole agent tree, not once per run.

        Returns:
            ``True`` for the first call at or above the warning threshold, and
            ``False`` before the threshold or after the warning was emitted.
        """
        if self._warn_emitted:
            return False
        over_ratio = (
            self.max_tokens is not None and self.spent_tokens >= self.warn_ratio * self.max_tokens
        ) or (
            self.max_cost_usd is not None and self.spent_usd >= self.warn_ratio * self.max_cost_usd
        )
        if over_ratio:
            self._warn_emitted = True
            return True
        return False
