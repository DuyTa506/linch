from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any, TypeGuard

logger = logging.getLogger(__name__)

_DEFAULT_CONTEXT_WINDOW = 128_000
_SOLE_MODEL = "*"


async def probe_context_windows(
    get_client: Callable[[], Awaitable[Any]],
    *,
    base_url: str | None,
    timeout: float,
) -> dict[str, int]:
    """Read positive per-model ``max_model_len`` values through the chat client.

    Args:
        get_client: Adapter factory for the client that serves chat, so the probe
            shares its credentials, base path, and redirect policy.
        base_url: Configured endpoint; without it or ``OPENAI_BASE_URL`` the client
            targets OpenAI itself, which is never probed.
        timeout: Limit in seconds for the whole probe, including SDK retries.

    Returns:
        Capacity by model id, empty when the probe fails or the server omits it.
    """
    if not (base_url or os.environ.get("OPENAI_BASE_URL")):
        return {}
    try:
        async with asyncio.timeout(timeout):
            client = await get_client()
            page = await client.models.list()
        rows = list(page.data)
    except Exception:
        logger.debug("Context window probe failed", exc_info=True)
        return {}
    result: dict[str, int] = {}
    for row in rows:
        model = getattr(row, "id", None)
        value = getattr(row, "max_model_len", None)
        if not isinstance(model, str) or not model:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            continue
        result[model] = value
        if len(rows) == 1:
            result[_SOLE_MODEL] = value
    return result


def valid_context_window(value: object) -> TypeGuard[int]:
    """Return whether a configured capacity is a positive non-boolean integer."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def probed_context_window(cache: dict[str, int] | None, model: str) -> int:
    """Return the probed capacity, trusting a single-model server for any alias."""
    cache = cache or {}
    return cache.get(model, cache.get(_SOLE_MODEL, _DEFAULT_CONTEXT_WINDOW))
