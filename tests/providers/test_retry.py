from __future__ import annotations

import pytest

from linch.abort import AbortContext
from linch.errors import AbortError
from linch.providers.retry import RetryOptions, with_retry


class _Retryable(Exception):
    retryable = True


_FAST = RetryOptions(max_attempts=5, base_delay_ms=0, max_delay_ms=0, jitter=0.0)


def test_retry_after_zero_is_honored_not_dropped() -> None:
    """Retry-After: 0 ('retry now') must parse to 0.0, not be treated as missing."""
    from linch._http_errors import retry_after_seconds

    class Resp:
        headers = {"retry-after": 0}

    class Err(Exception):
        response = Resp()

    assert retry_after_seconds(Err("rate limited")) == 0.0


def test_delay_for_error_uses_zero_retry_after_for_immediate_retry() -> None:
    """A RateLimitError with retry_after_seconds == 0.0 retries immediately."""
    from linch.errors import RateLimitError
    from linch.providers.retry import _delay_for_error

    err = RateLimitError("slow down", retry_after_seconds=0.0)
    opts = RetryOptions(base_delay_ms=5000, max_delay_ms=30000, jitter=0.0)
    # Without the fix, the falsy 0.0 hint is ignored and a 5s backoff is used.
    assert _delay_for_error(err, attempt=0, opts=opts) == 0.0


@pytest.mark.asyncio
async def test_pre_aborted_context_does_not_call_fn():
    ctx = AbortContext()
    ctx.abort()
    calls = 0

    async def fn(attempt: int) -> str:
        nonlocal calls
        calls += 1
        return "ok"

    with pytest.raises(AbortError):
        await with_retry(fn, signal=ctx, options=_FAST)
    assert calls == 0


@pytest.mark.asyncio
async def test_abort_between_retries_stops_retrying():
    ctx = AbortContext()
    calls = 0

    async def fn(attempt: int) -> str:
        nonlocal calls
        calls += 1
        ctx.abort()
        raise _Retryable("boom")

    with pytest.raises(AbortError):
        await with_retry(fn, signal=ctx, options=_FAST)
    assert calls == 1


@pytest.mark.asyncio
async def test_no_signal_normal_retry_path():
    calls = 0

    async def fn(attempt: int) -> str:
        nonlocal calls
        calls += 1
        if attempt == 0:
            raise _Retryable("boom")
        return "ok"

    result = await with_retry(fn, signal=None, options=_FAST)
    assert result == "ok"
    assert calls == 2
