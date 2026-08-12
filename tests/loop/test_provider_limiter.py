"""`Agent(limiter=...)` — the proactive gate around every live provider call.

The gate sits inside `stream_turn` (so it is released across retry backoff) and
around `strategy.compact(...)` (so third-party compaction strategies are covered
too). These tests pin both placements, the balanced release on every exit path,
and the fact that an unset limiter costs nothing.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from linch import Agent
from linch.errors import ContextLengthError, ProviderError
from linch.providers import BaseProvider
from linch.sessions import InMemorySessionStore
from linch.types import Usage


class SpyLimiter:
    """Records every acquire/release and tracks how many slots were held at once."""

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
    """A SpyLimiter that actually blocks past *cap* concurrent holders."""

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


class TextProvider(BaseProvider):
    """One text-only turn per call, optionally sleeping to overlap with peers."""

    id = "fake"

    def __init__(self, *, delay: float = 0.0) -> None:
        self.calls = 0
        self._delay = delay

    def context_window(self, model: str) -> int:
        return 100_000

    async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
        self.calls += 1
        yield {"type": "message_start", "model": req.model}
        if self._delay:
            await asyncio.sleep(self._delay)
        yield {"type": "text_delta", "text": "done"}
        yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}


class FailingProvider(BaseProvider):
    """Raises *error* on the first call, then behaves like TextProvider."""

    id = "fake"

    def __init__(self, error: BaseException) -> None:
        self.calls = 0
        self._error = error

    def context_window(self, model: str) -> int:
        return 100_000

    async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
        self.calls += 1
        yield {"type": "message_start", "model": req.model}
        if self.calls == 1:
            raise self._error
        yield {"type": "text_delta", "text": "recovered"}
        yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}


def _agent(provider: BaseProvider, **kwargs: Any) -> Agent:
    return Agent(
        model="gpt-5",
        provider=provider,
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        cwd=".",
        **kwargs,
    )


async def _drain(agent: Agent, prompt: str = "hi") -> list[Any]:
    session = await agent.session()
    return [event async for event in session.run(prompt)]


async def _fan_out(agent: Agent, n: int) -> None:
    """Run *n* sessions of one agent concurrently.

    Sessions are created serially on purpose: `agent.session()` is not safe to
    call from concurrent tasks (its lazy `connect_skills` coroutine is awaited
    twice), which is unrelated to what these tests measure.
    """
    sessions = [await agent.session() for _ in range(n)]

    async def drive(session: Any) -> None:
        async for _ in session.run("hi"):
            pass

    await asyncio.gather(*(drive(s) for s in sessions))


async def _fan_out_errors(agent: Agent, n: int) -> list[str]:
    """Run *n* sessions concurrently and return the name of every error event."""
    sessions = [await agent.session() for _ in range(n)]

    async def drive(session: Any) -> list[str]:
        return [
            str(event.error.get("name", "error"))
            async for event in session.run("hi")
            if event.type == "error"
        ]

    return [name for batch in await asyncio.gather(*(drive(s) for s in sessions)) for name in batch]


async def test_limiter_is_acquired_and_released_around_a_turn() -> None:
    limiter = SpyLimiter()
    events = await _drain(_agent(TextProvider(), limiter=limiter))

    assert events[-1].subtype == "success"
    assert limiter.acquired == ["gpt-5"]
    assert limiter.released == ["gpt-5"]


async def test_limiter_is_released_when_the_provider_raises() -> None:
    limiter = SpyLimiter()
    provider = FailingProvider(ProviderError("boom", status=400, retryable=False))

    await _drain(_agent(provider, limiter=limiter))

    assert limiter.acquired, "the gate was never entered"
    assert limiter.balanced, "a failed provider call leaked its slot"


async def test_limiter_is_released_across_retry_backoff() -> None:
    """Pins D5: a retrying call re-queues instead of holding its slot while sleeping.

    With a cap of one, a slot held across `_retry_same_model`'s backoff would
    make the second attempt wait on itself forever.
    """
    limiter = CappingLimiter(cap=1)
    provider = FailingProvider(ProviderError("overloaded", status=529, retryable=True))

    events = await asyncio.wait_for(
        _drain(_agent(provider, limiter=limiter, max_retries=3)), timeout=5.0
    )

    assert provider.calls == 2, "the retry never happened"
    assert events[-1].subtype == "success"
    assert len(limiter.acquired) == 2, "the retry did not re-enter the gate"
    assert limiter.high_water == 1
    assert limiter.balanced


async def test_an_unset_limiter_resolves_to_a_no_op_context() -> None:
    """Pins the zero-overhead rule: no limiter means no semaphore, no wrapper."""
    from contextlib import nullcontext

    from linch.providers.limiter import provider_slot

    agent = _agent(TextProvider())
    assert agent.limiter is None
    assert isinstance(provider_slot(agent, "gpt-5"), type(nullcontext()))


async def test_cap_bounds_concurrent_provider_calls_across_sessions() -> None:
    """The whole point: one ceiling shared by every session of one Agent."""
    capped = CappingLimiter(cap=2)
    agent = _agent(TextProvider(delay=0.05), limiter=capped)
    await _fan_out(agent, 5)
    assert capped.high_water == 2
    assert capped.balanced

    # Control: the same fan-out with a non-blocking limiter really does overlap,
    # so the assertion above is measuring the cap and not the test's own timing.
    uncapped = SpyLimiter()
    loose = _agent(TextProvider(delay=0.05), limiter=uncapped)
    await _fan_out(loose, 5)
    assert uncapped.high_water == 5


async def test_compaction_goes_through_the_gate() -> None:
    """Pins D3: the gate wraps `strategy.compact`, so custom strategies are covered."""
    from linch.abort import AbortContext
    from linch.compaction import run_forced_compaction

    limiter = SpyLimiter()
    seen: list[int] = []

    class RecordingStrategy:
        id = "recording"

        async def compact(self, ctx: Any, provider: Any) -> list[Any]:
            seen.append(limiter.in_flight)
            return list(ctx.messages)

    agent = _agent(TextProvider(), limiter=limiter, compaction=RecordingStrategy())
    session = await agent.session()
    await _drain_session(session)
    limiter.acquired.clear()
    limiter.released.clear()

    await run_forced_compaction(session, agent, AbortContext())

    assert seen == [1], "compaction ran outside the gate"
    assert limiter.balanced


async def test_forced_compaction_inside_a_turn_does_not_deadlock() -> None:
    """The headline safety test.

    Forced compaction is invoked from inside the turn path. At a cap of one this
    only works because `stream_turn` releases its slot on the way out of the
    generator, before `run_forced_compaction` asks for one.
    """
    limiter = CappingLimiter(cap=1)
    provider = FailingProvider(ContextLengthError("context too long"))

    events = await asyncio.wait_for(_drain(_agent(provider, limiter=limiter)), timeout=5.0)

    assert provider.calls == 2
    assert events[-1].subtype == "success"
    assert limiter.balanced


async def test_abandoning_the_run_releases_the_slot() -> None:
    limiter = SpyLimiter()
    agent = _agent(TextProvider(delay=0.02), limiter=limiter)
    session = await agent.session()

    stream = session.run("hi")
    await stream.__anext__()
    await stream.aclose()

    assert limiter.balanced, "abandoning the run leaked a slot"


async def _drain_session(session: Any) -> None:
    async for _ in session.run("hi"):
        pass


async def test_both_limiter_and_max_provider_concurrency_is_rejected() -> None:
    from linch.errors import ConfigError

    with pytest.raises(ConfigError, match="max_provider_concurrency"):
        _agent(TextProvider(), limiter=SpyLimiter(), max_provider_concurrency=2)


async def test_max_provider_concurrency_caps_like_an_explicit_limiter() -> None:
    agent = _agent(TextProvider(delay=0.05), max_provider_concurrency=2)
    assert agent.limiter is not None

    running = 0
    high_water = 0
    original = agent.provider.stream

    def tracked(req: Any) -> AsyncIterator[dict[str, object]]:
        async def run() -> AsyncIterator[dict[str, object]]:
            nonlocal running, high_water
            running += 1
            high_water = max(high_water, running)
            try:
                async for item in original(req):
                    yield item
            finally:
                running -= 1

        return run()

    agent.provider.stream = tracked  # type: ignore[method-assign]
    await _fan_out(agent, 5)

    assert high_water == 2


async def test_max_provider_concurrency_unset_leaves_no_limiter() -> None:
    assert _agent(TextProvider()).limiter is None


def test_the_cap_survives_being_used_from_a_new_event_loop() -> None:
    """An `Agent` reused across loops must not trip over its own semaphore.

    `asyncio.Semaphore` binds to the loop of its first *contended* acquire, so
    the failure only shows up under load: an uncapped second loop works, a
    contended one raises "bound to a different event loop". A host that runs one
    loop per task (Celery) is exactly the shape `Agent(max_provider_concurrency=)`
    is for, so the cap has to outlive the loop it was first used on.
    """
    agent = _agent(TextProvider(delay=0.01), max_provider_concurrency=1)

    def drive() -> list[str]:
        # Three sessions against a cap of one guarantees contention. The loop
        # turns a limiter failure into an error event rather than raising, so
        # assert on the events: otherwise two of three runs fail silently.
        return asyncio.run(_fan_out_errors(agent, 3))

    assert drive() == []
    assert drive() == []


def test_the_cap_still_binds_on_the_second_loop() -> None:
    """Rebuilding the semaphore must not quietly hand the new loop a free pass."""
    provider = TextProvider(delay=0.01)
    agent = _agent(provider, max_provider_concurrency=1)

    running = 0
    high_water = 0
    original = provider.stream

    def tracked(req: Any) -> AsyncIterator[dict[str, object]]:
        async def run() -> AsyncIterator[dict[str, object]]:
            nonlocal running, high_water
            running += 1
            high_water = max(high_water, running)
            try:
                async for item in original(req):
                    yield item
            finally:
                running -= 1

        return run()

    provider.stream = tracked  # type: ignore[method-assign]

    assert asyncio.run(_fan_out_errors(agent, 3)) == []
    high_water = 0
    assert asyncio.run(_fan_out_errors(agent, 3)) == []

    assert high_water == 1


def test_rebuilding_is_refused_while_slots_are_still_held() -> None:
    """Two live loops sharing one cap is ambiguous — say so instead of doubling it."""
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
