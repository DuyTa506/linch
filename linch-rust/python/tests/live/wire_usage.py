"""Read a field of a raw OpenAI-compatible `usage` object, whichever server wrote it."""

from __future__ import annotations

from typing import Any


def reasoning_tokens(usage: dict[str, Any] | None) -> int | None:
    """The reasoning-token count of a raw `usage` object, or None when it reports none.

    Args:
        usage: The `usage` object as the server sent it. vLLM puts the count at
            `completion_tokens_details.reasoning_tokens`; SGLang at `reasoning_tokens` and
            leaves `completion_tokens_details` null.

    Returns:
        The count, or None when neither location holds one.
    """
    if not usage:
        return None
    details = usage.get("completion_tokens_details") or {}
    count = details.get("reasoning_tokens")
    return count if count is not None else usage.get("reasoning_tokens")
