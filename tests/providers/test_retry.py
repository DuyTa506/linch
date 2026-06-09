from __future__ import annotations

import pytest

from linch.abort import AbortContext
from linch.errors import AbortError
from linch.providers.retry import RetryOptions, with_retry


class _Retryable(Exception):
    retryable = True


_FAST = RetryOptions(max_attempts=5, base_delay_ms=0, max_delay_ms=0, jitter=0.0)


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
