"""Token-count → USD cost helpers.

Pricing is per 1 million tokens.  The table mirrors the pattern of
``_KNOWN_CONTEXT`` in ``providers/anthropic.py`` — a plain dict seeded with
published rates, pluggable via the ``table=`` kwarg or direct mutation of
``_DEFAULT_PRICING``.  Unknown models return ``None`` (never silently "free").

Cache write rates use the 5-minute TTL tier.  Callers using 1-hour TTL caching
should pass a custom ``table`` with the 2× creation rate.

Rates sourced 2026-06-08 from https://www.anthropic.com/pricing
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .types import Usage

_M = 1_000_000  # per-million denominator


@dataclass(frozen=True, slots=True)
class ModelPricing:
    """USD-per-1M-token rates for a single model.

    Attributes:
        input:            Standard prompt tokens.
        output:           Generated tokens.
        cache_read:       Prompt-cache hit tokens (cheaper than input).
        cache_creation:   Prompt-cache write tokens, 5-minute TTL.
    """

    input: float
    output: float
    cache_read: float
    cache_creation: float


# Rates sourced 2026-06-08.  cache_read ≈ 0.1× input; cache_creation ≈ 1.25× input.
_DEFAULT_PRICING: dict[str, ModelPricing] = {
    # Claude Opus 4.x
    "claude-opus-4-8": ModelPricing(input=5.00, output=25.00, cache_read=0.50, cache_creation=6.25),
    "claude-opus-4-7": ModelPricing(input=5.00, output=25.00, cache_read=0.50, cache_creation=6.25),
    "claude-opus-4-6": ModelPricing(input=5.00, output=25.00, cache_read=0.50, cache_creation=6.25),
    # Claude Sonnet 4.x
    "claude-sonnet-4-6": ModelPricing(
        input=3.00, output=15.00, cache_read=0.30, cache_creation=3.75
    ),
    # Claude Haiku 4.x
    "claude-haiku-4-5": ModelPricing(input=1.00, output=5.00, cache_read=0.10, cache_creation=1.25),
    "claude-haiku-4-5-20251001": ModelPricing(
        input=1.00, output=5.00, cache_read=0.10, cache_creation=1.25
    ),
}


def cost_usd(
    usage: Usage,
    model: str,
    table: dict[str, ModelPricing] | None = None,
) -> float | None:
    """Return the USD cost of *usage* for *model*, or ``None`` for unknown models.

    All four token buckets are summed independently:
    - ``input_tokens``          — standard prompt tokens
    - ``output_tokens``         — generated tokens
    - ``cache_read_tokens``     — prompt-cache hits (cheap)
    - ``cache_creation_tokens`` — prompt-cache writes (slightly more expensive)

    Anthropic reports these as separate, non-overlapping fields; summing all
    four is correct.  Verify this assumption when adding OpenAI entries — if
    any provider folds cached tokens into ``input_tokens``, adjust the formula
    to avoid double-counting.

    Args:
        usage: Token counts for a single turn or an accumulated total.
        model: The exact model ID string (e.g. ``"claude-sonnet-4-6"``).
        table: Optional custom pricing table; defaults to ``_DEFAULT_PRICING``.

    Returns:
        USD cost as a float, or ``None`` if *model* is not in the table.
    """
    pricing_table = table if table is not None else _DEFAULT_PRICING
    pricing = pricing_table.get(model)
    if pricing is None:
        return None
    return (
        usage.input_tokens * pricing.input / _M
        + usage.output_tokens * pricing.output / _M
        + usage.cache_read_tokens * pricing.cache_read / _M
        + usage.cache_creation_tokens * pricing.cache_creation / _M
    )
