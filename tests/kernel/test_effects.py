from __future__ import annotations

import asyncio

import pytest

from linch.kernel import Disposable, EffectScope


@pytest.mark.asyncio
async def test_disposable_runs_once() -> None:
    calls: list[int] = []
    d = Disposable(lambda: calls.append(1))
    await d.dispose()
    await d.dispose()  # idempotent
    assert calls == [1]
    assert d.disposed is True


@pytest.mark.asyncio
async def test_disposable_callable_sugar() -> None:
    calls: list[int] = []
    d = Disposable(lambda: calls.append(1))
    await d()  # await the disposable directly
    assert calls == [1]


@pytest.mark.asyncio
async def test_disposable_awaits_async_teardown() -> None:
    calls: list[str] = []

    async def teardown() -> None:
        calls.append("async")

    d = Disposable(teardown)
    await d.dispose()
    assert calls == ["async"]


@pytest.mark.asyncio
async def test_effect_disposes_in_reverse_order() -> None:
    order: list[int] = []
    scope = EffectScope()
    for i in range(3):
        scope.effect(lambda i=i: lambda: order.append(i))
    await scope.dispose()
    assert order == [2, 1, 0]


@pytest.mark.asyncio
async def test_effect_group_disposer_is_scoped() -> None:
    order: list[str] = []
    scope = EffectScope()
    scope.effect(lambda: lambda: order.append("outer"))
    handle = scope.effect(lambda: lambda: order.append("inner"))
    await handle.dispose()  # only the inner effect
    assert order == ["inner"]
    await scope.dispose()  # outer still runs; inner not repeated
    assert order == ["inner", "outer"]


@pytest.mark.asyncio
async def test_effect_accepts_iterable_of_disposers() -> None:
    order: list[int] = []
    scope = EffectScope()

    def register() -> list:
        return [lambda: order.append(1), lambda: order.append(2)]

    scope.effect(register)
    await scope.dispose()
    assert order == [2, 1]  # reverse within the group


@pytest.mark.asyncio
async def test_effect_awaits_async_disposer() -> None:
    order: list[str] = []
    scope = EffectScope()

    async def teardown() -> None:
        order.append("async")

    scope.effect(lambda: teardown)
    await scope.dispose()
    assert order == ["async"]


@pytest.mark.asyncio
async def test_scope_dispose_is_idempotent() -> None:
    calls: list[int] = []
    scope = EffectScope()
    scope.effect(lambda: lambda: calls.append(1))
    await scope.dispose()
    await scope.dispose()
    assert calls == [1]


@pytest.mark.asyncio
async def test_concurrent_dispose_coalesces_and_shields_teardown() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def teardown() -> None:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()

    disposable = Disposable(teardown)
    cancelled_waiter = asyncio.create_task(disposable.dispose())
    await started.wait()
    other_waiter = asyncio.create_task(disposable.dispose())

    cancelled_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_waiter

    release.set()
    await other_waiter
    assert calls == 1
    assert disposable.disposed is True


@pytest.mark.asyncio
async def test_cleanup_failure_is_shared_then_retryable() -> None:
    attempts = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def teardown() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            started.set()
            await release.wait()
            raise RuntimeError("cleanup failed")

    disposable = Disposable(teardown)
    first = asyncio.create_task(disposable.dispose())
    await started.wait()
    second = asyncio.create_task(disposable.dispose())
    release.set()

    results = await asyncio.gather(first, second, return_exceptions=True)
    assert all(isinstance(result, RuntimeError) for result in results)
    assert attempts == 1
    assert disposable.disposed is False

    await disposable.dispose()
    assert attempts == 2
    assert disposable.disposed is True


@pytest.mark.asyncio
async def test_scope_attempts_all_cleanup_and_retries_only_failures() -> None:
    order: list[str] = []
    failed_once = False
    scope = EffectScope()

    def flaky() -> None:
        nonlocal failed_once
        order.append("flaky")
        if not failed_once:
            failed_once = True
            raise RuntimeError("try again")

    scope.add(lambda: order.append("first"))
    scope.add(flaky)
    scope.add(lambda: order.append("last"))

    with pytest.raises(RuntimeError, match="try again"):
        await scope.dispose()
    assert order == ["last", "flaky", "first"]
    assert scope.closed is True
    assert scope.disposed is False

    await scope.dispose()
    assert order == ["last", "flaky", "first", "flaky"]
    assert scope.disposed is True


@pytest.mark.asyncio
async def test_scope_rejects_add_and_effect_once_teardown_starts() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    setup_called = False
    scope = EffectScope()

    async def teardown() -> None:
        started.set()
        await release.wait()

    scope.add(teardown)
    disposing = asyncio.create_task(scope.dispose())
    await started.wait()

    with pytest.raises(RuntimeError, match="disposing or disposed"):
        scope.add(lambda: None)

    def setup() -> None:
        nonlocal setup_called
        setup_called = True

    with pytest.raises(RuntimeError, match="disposing or disposed"):
        scope.effect(setup)
    assert setup_called is False

    release.set()
    await disposing


def test_effect_rejects_invalid_iterable_disposer_during_registration() -> None:
    scope = EffectScope()

    with pytest.raises(TypeError, match="disposer must be callable"):
        scope.effect(lambda: [lambda: None, 42])
