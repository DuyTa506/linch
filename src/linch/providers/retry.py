from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from linch.errors import AbortError, ConfigError, RateLimitError

T = TypeVar("T")


@dataclass(slots=True)
class RetryOptions:
    max_attempts: int = 5
    base_delay_ms: int = 1000
    max_delay_ms: int = 30000
    jitter: float = 0.2


def _delay_for_error(err: Exception, attempt: int, opts: RetryOptions) -> float:
    if isinstance(err, RateLimitError) and getattr(err, "retry_after_seconds", None):
        return min(float(err.retry_after_seconds) * 1000.0, float(opts.max_delay_ms))
    exp = min(float(opts.base_delay_ms) * (2**attempt), float(opts.max_delay_ms))
    jitter = exp * opts.jitter * ((random.random() * 2.0) - 1.0)
    return max(0.0, exp + jitter)


async def with_retry(
    fn: Callable[[int], Awaitable[T]],
    *,
    signal: object | None,
    options: RetryOptions | None = None,
) -> T:
    opts = options or RetryOptions()
    if opts.max_attempts < 1:
        raise ConfigError("max_attempts must be >= 1")
    last_error: Exception | None = None
    for attempt in range(opts.max_attempts):
        if getattr(signal, "is_set", False):
            raise AbortError("aborted")
        try:
            return await fn(attempt)
        except AbortError:
            raise
        except Exception as exc:
            last_error = exc
            retryable = bool(getattr(exc, "retryable", False))
            if not retryable or attempt == opts.max_attempts - 1:
                raise
            delay_ms = _delay_for_error(exc, attempt, opts)
            await asyncio.sleep(delay_ms / 1000.0)
    if last_error is not None:
        raise last_error
    raise RuntimeError("retry failed without error")
