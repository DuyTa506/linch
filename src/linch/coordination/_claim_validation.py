"""Common argument validation for leased coordination deliveries."""

from __future__ import annotations

import math


def _validate_claim_args(*, owner: str, lease_s: float, now: float, limit: int | None) -> None:
    if not isinstance(owner, str) or not owner:
        raise ValueError("owner must be non-empty")
    if isinstance(lease_s, bool) or not isinstance(lease_s, (int, float)):
        raise ValueError("lease_s must be a positive finite number")
    if not math.isfinite(lease_s) or lease_s <= 0:
        raise ValueError("lease_s must be a positive finite number")
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
        raise ValueError("now must be finite")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 0):
        raise ValueError("limit must be a non-negative integer or None")
