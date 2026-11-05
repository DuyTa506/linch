from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from linch.errors import ContextLengthError
from linch.providers import BaseProvider
from linch.types import Usage
from tests.support.agents import make_agent


class SpyLimiter:
    def __init__(self) -> None:
        self.acquired: list[str] = []
        self.released: list[str] = []
        self.in_flight = 0
        self.high_water = 0

    async def acquire(self, *, model: str) -> None:
        self.acquired.append(model)
        self.in_flight += 1
        self.high_water = max(self.high_water, self.in_flight)

    def release(self, *, model: str) -> None:
        self.released.append(model)
        self.in_flight -= 1

    @property
    def balanced(self) -> bool:
        return self.in_flight == 0 and len(self.acquired) == len(self.released)


class CappingLimiter(SpyLimiter):
    def __init__(self, cap: int) -> None:
        super().__init__()
        self._cap = cap
        self._sem: asyncio.Semaphore | None = None

    async def acquire(self, *, model: str) -> None:
        if self._sem is None:
            self._sem = asyncio.Semaphore(self._cap)
        await self._sem.acquire()
        await super().acquire(model=model)

    def release(self, *, model: str) -> None:
        super().release(model=model)
        assert self._sem is not None
        self._sem.release()


class LimiterProvider(BaseProvider):
    id = "fake"

    def __init__(self, error: BaseException | None = None, *, delay: float = 0.0) -> None:
        self.calls = 0
        self._error = error
        self._delay = delay

    def context_window(self, model: str) -> int:
        return 100_000

    async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
        self.calls += 1
        yield {"type": "message_start", "model": req.model}
        if self.calls == 1 and self._error is not None:
            raise self._error
        if self._delay:
            await asyncio.sleep(self._delay)
        yield {"type": "text_delta", "text": "recovered" if self._error else "done"}
        yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}


def _agent(provider: BaseProvider, **kwargs: Any) -> Any:
    return make_agent(provider, model="gpt-5", run_store=None, **kwargs)


async def _drain(agent: Any, prompt: str = "hi") -> list[Any]:
    session = await agent.session()
    return [event async for event in session.run(prompt)]


async def _fan_out(agent: Any, n: int) -> None:
    sessions = [await agent.session() for _ in range(n)]

    async def drive(session: Any) -> None:
        async for _ in session.run("hi"):
            pass

    await asyncio.gather(*(drive(s) for s in sessions))


def _track(provider: Any) -> dict[str, int]:
    stats = {"running": 0, "high_water": 0}
    original = provider.stream

    def tracked(req: Any) -> AsyncIterator[dict[str, object]]:
        async def run() -> AsyncIterator[dict[str, object]]:
            stats["running"] += 1
            stats["high_water"] = max(stats["high_water"], stats["running"])
            try:
                async for item in original(req):
                    yield item
            finally:
                stats["running"] -= 1

        return run()

    provider.stream = tracked  # type: ignore[method-assign]
    return stats


@pytest.mark.parametrize(
    ("error", "max_retries", "assert_retry_slot"),
    [
        (ContextLengthError("context too long"), 0, False),
    ],
    ids=["forced-compaction"],
)
async def test_provider_slot_is_released_before_retry_or_compaction(
    error: BaseException, max_retries: int, assert_retry_slot: bool
) -> None:
    limiter = CappingLimiter(cap=1)
    provider = LimiterProvider(error)

    events = await asyncio.wait_for(
        _drain(_agent(provider, limiter=limiter, max_retries=max_retries)), timeout=5.0
    )

    assert provider.calls == 2
    assert events[-1].subtype == "success"
    assert limiter.balanced
    if assert_retry_slot:
        assert len(limiter.acquired) == 2
        assert limiter.high_water == 1


async def test_both_limiter_and_max_provider_concurrency_is_rejected() -> None:
    from linch.errors import ConfigError

    with pytest.raises(ConfigError, match="max_provider_concurrency"):
        _agent(LimiterProvider(), limiter=SpyLimiter(), max_provider_concurrency=2)


async def test_max_provider_concurrency_caps_like_an_explicit_limiter() -> None:
    agent = _agent(LimiterProvider(delay=0.05), max_provider_concurrency=2)
    assert agent.limiter is not None

    stats = _track(agent.provider)
    await _fan_out(agent, 5)

    assert stats["high_water"] == 2


def test_rebuilding_is_refused_while_slots_are_still_held() -> None:
    from linch.errors import ConfigError
    from linch.providers.limiter import _SemaphoreLimiter

    limiter = _SemaphoreLimiter(2)

    async def take_one() -> None:
        await limiter.acquire(model="m")  # deliberately never released

    asyncio.run(take_one())

    async def take_another() -> None:
        await limiter.acquire(model="m")

    with pytest.raises(ConfigError, match="event loop"):
        asyncio.run(take_another())
