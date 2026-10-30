"""Custom estimator invocation shared by otherwise distinct counting policies."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from .types import Message


def custom_token_estimate(
    estimator: Any, messages: Sequence[Message], model: str | None
) -> int | None:
    """Return a nonnegative estimate, or let the caller choose its fallback."""
    if callable(estimator):
        try:
            return max(0, int(cast(Any, estimator(messages, model))))
        except Exception:
            pass
    return None
