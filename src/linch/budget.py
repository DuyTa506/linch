"""Run budgets — token/USD spending caps shared across an agent tree.

A :class:`RunBudget` is a plain mutable accumulator.  The run loop charges it
after every provider turn and checks :attr:`RunBudget.exceeded` before each
provider call; subagent child runs inherit the *same object* from their parent
session, so one budget caps the whole tree (parent + all workers).

Token counts sum all four :class:`~linch.types.Usage` buckets (input, output,
cache read, cache creation), matching ``Usage.add`` semantics.  USD cost comes
from :func:`linch.pricing.cost_usd`; unknown models cost ``None`` and charge
``0.0`` — for unpriced models only the token limit binds.

The event loop is single-threaded, so plain counters are safe — no locks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .types import Usage


@dataclass(slots=True)
class RunBudget:
    """Spending cap for a run (and its subagent tree).

    Attributes:
        max_tokens: Cap on total tokens (all four ``Usage`` buckets summed),
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

    def charge(self, usage: Usage, cost_usd: float | None) -> None:
        """Record one turn's spending. ``None`` cost (unknown model) charges 0."""
        self.spent_tokens += (
            usage.input_tokens
            + usage.output_tokens
            + usage.cache_read_tokens
            + usage.cache_creation_tokens
        )
        if cost_usd is not None:
            self.spent_usd += cost_usd

    @property
    def remaining_tokens(self) -> int | None:
        if self.max_tokens is None:
            return None
        return max(0, self.max_tokens - self.spent_tokens)

    @property
    def remaining_usd(self) -> float | None:
        if self.max_cost_usd is None:
            return None
        return max(0.0, self.max_cost_usd - self.spent_usd)

    @property
    def exceeded(self) -> bool:
        if self.max_tokens is not None and self.spent_tokens >= self.max_tokens:
            return True
        if self.max_cost_usd is not None and self.spent_usd >= self.max_cost_usd:
            return True
        return False

    def take_warning(self) -> bool:
        """Return ``True`` exactly once, when spending first reaches
        ``warn_ratio`` of any configured limit.

        Shared budgets warn once across the whole agent tree, not once per run.
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
