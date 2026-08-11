"""WorkflowContext combinator tests (no provider needed).

linch imports happen inside test functions because tests/loop/test_hardening.py
pops all ``linch*`` modules from ``sys.modules``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest


def _make_context(**kwargs: Any) -> Any:
    from linch.workflow import WorkflowContext

    return WorkflowContext(agent=None, host_session=None, **kwargs)


def _pending_tasks() -> list[Any]:
    current = asyncio.current_task()
    return [t for t in asyncio.all_tasks() if not t.done() and t is not current]


async def test_parallel_respects_semaphore_cap() -> None:
    wf = _make_context(max_concurrency=2)
    active = 0
    high_water = 0

    def make_thunk(i: int):
        async def thunk() -> int:
            nonlocal active, high_water
            active += 1
            high_water = max(high_water, active)
            await asyncio.sleep(0.01)
            active -= 1
            return i

        return thunk

    results = await wf.parallel([make_thunk(i) for i in range(6)])

    assert results == [0, 1, 2, 3, 4, 5]
    assert high_water == 2


async def test_parallel_preserves_result_order() -> None:
    wf = _make_context(max_concurrency=8)

    def make_thunk(i: int):
        async def thunk() -> int:
            # Later items finish first; result order must still match input.
            await asyncio.sleep((5 - i) * 0.005)
            return i

        return thunk

    results = await wf.parallel([make_thunk(i) for i in range(5)])

    assert results == [0, 1, 2, 3, 4]


async def test_pipeline_chains_stages_per_item_without_barrier() -> None:
    wf = _make_context(max_concurrency=8)
    timeline: list[str] = []

    async def stage1(item: str) -> str:
        await asyncio.sleep(0.03 if item == "slow" else 0.001)
        timeline.append(f"s1:{item}")
        return f"{item}+1"

    async def stage2(value: str) -> str:
        timeline.append(f"s2:{value}")
        return f"{value}+2"

    results = await wf.pipeline(["slow", "fast"], stage1, stage2)

    assert results == ["slow+1+2", "fast+1+2"]
    # No barrier: the fast item's stage 2 ran before the slow item's stage 1.
    assert timeline.index("s2:fast+1") < timeline.index("s1:slow")


async def test_parallel_cancels_siblings_when_a_branch_raises() -> None:
    wf = _make_context(max_concurrency=8)
    finished: list[str] = []
    cleaned: list[str] = []

    def make_slow(name: str):
        async def thunk() -> str:
            try:
                await asyncio.sleep(1.0)
                finished.append(name)
                return name
            finally:
                cleaned.append(name)

        return thunk

    async def boom() -> str:
        await asyncio.sleep(0.01)
        raise RuntimeError("branch failed")

    with pytest.raises(RuntimeError, match="branch failed"):
        await wf.parallel([make_slow("b0"), boom, make_slow("b2")])

    assert finished == []
    assert sorted(cleaned) == ["b0", "b2"]


async def test_parallel_leaks_no_tasks_after_branch_failure() -> None:
    wf = _make_context(max_concurrency=8)

    async def slow() -> None:
        await asyncio.sleep(1.0)

    async def boom() -> None:
        await asyncio.sleep(0.01)
        raise RuntimeError("branch failed")

    with pytest.raises(RuntimeError):
        await wf.parallel([slow, boom, slow])

    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert _pending_tasks() == []


async def test_parallel_caller_cancellation_drains_branches() -> None:
    wf = _make_context(max_concurrency=8)
    cleaned: list[str] = []
    started = asyncio.Event()

    def make_branch(name: str):
        async def thunk() -> None:
            started.set()
            try:
                await asyncio.sleep(1.0)
            finally:
                cleaned.append(name)

        return thunk

    task = asyncio.ensure_future(wf.parallel([make_branch("b0"), make_branch("b1")]))
    await asyncio.wait_for(started.wait(), timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert sorted(cleaned) == ["b0", "b1"]
    await asyncio.sleep(0)
    assert _pending_tasks() == []


async def test_nested_parallel_does_not_deadlock() -> None:
    wf = _make_context(max_concurrency=2)

    def make_leaf(i: int):
        async def leaf() -> int:
            await asyncio.sleep(0.01)
            return i

        return leaf

    def make_branch():
        async def branch() -> list[int]:
            return await wf.parallel([make_leaf(0), make_leaf(1)])

        return branch

    results = await asyncio.wait_for(wf.parallel([make_branch(), make_branch()]), timeout=2.0)

    assert results == [[0, 1], [0, 1]]


async def test_nested_parallel_caps_each_level_independently() -> None:
    wf = _make_context(max_concurrency=2)
    outer_active = 0
    outer_high = 0
    inner_high: list[int] = []

    def make_branch():
        async def branch() -> None:
            nonlocal outer_active, outer_high
            outer_active += 1
            outer_high = max(outer_high, outer_active)
            active = 0
            high = 0

            def make_leaf():
                async def leaf() -> None:
                    nonlocal active, high
                    active += 1
                    high = max(high, active)
                    await asyncio.sleep(0.01)
                    active -= 1

                return leaf

            await wf.parallel([make_leaf() for _ in range(4)])
            inner_high.append(high)
            outer_active -= 1

        return branch

    await asyncio.wait_for(wf.parallel([make_branch() for _ in range(3)]), timeout=2.0)

    assert outer_high == 2
    assert inner_high == [2, 2, 2]


async def test_nested_pipeline_inside_parallel_does_not_deadlock() -> None:
    wf = _make_context(max_concurrency=2)

    async def stage(item: int) -> int:
        await asyncio.sleep(0.01)
        return item + 1

    def make_branch():
        async def branch() -> list[int]:
            return await wf.pipeline([1, 2], stage)

        return branch

    results = await asyncio.wait_for(wf.parallel([make_branch(), make_branch()]), timeout=2.0)

    assert results == [[2, 3], [2, 3]]


async def test_step_runs_sync_and_async_fn_and_returns_value() -> None:
    wf = _make_context()

    async def async_fn() -> dict[str, int]:
        return {"n": 1}

    assert await wf.step("sync", lambda: [1, 2]) == [1, 2]
    assert await wf.step("async", async_fn) == {"n": 1}


async def test_step_rejects_non_json_serializable_value() -> None:
    from linch.errors import ConfigError

    wf = _make_context()

    with pytest.raises(ConfigError, match="not JSON-serializable"):
        await wf.step("bad", object)


async def test_step_key_argument_distinguishes_calls() -> None:
    seen: list[Any] = []
    wf = _make_context(on_event=seen.append)

    await wf.step("fetch", lambda: "a", key="url-a")
    await wf.step("fetch", lambda: "b", key="url-b")

    ends = [e for e in seen if e.kind == "step_end"]
    assert len(ends) == 2
    assert ends[0].call_key != ends[1].call_key
    # Distinct keys, so both are the first occurrence of their own key.
    assert [e.occurrence for e in ends] == [0, 0]


async def test_step_timeout_raises_workflow_timeout_error() -> None:
    from linch.workflow import WorkflowTimeoutError

    wf = _make_context()

    async def slow() -> None:
        await asyncio.sleep(1.0)

    with pytest.raises(WorkflowTimeoutError, match="timed out"):
        await wf.step("slow", slow, timeout_ms=10)


async def test_step_timeout_falls_back_to_the_context_default() -> None:
    from linch.workflow import WorkflowTimeoutError

    wf = _make_context(step_timeout_ms=10)

    async def slow() -> None:
        await asyncio.sleep(1.0)

    with pytest.raises(WorkflowTimeoutError):
        await wf.step("slow", slow)


async def test_step_timeout_zero_opts_out_of_the_context_default() -> None:
    wf = _make_context(step_timeout_ms=10)

    async def slower_than_the_default() -> str:
        await asyncio.sleep(0.05)
        return "done"

    assert await wf.step("slow", slower_than_the_default, timeout_ms=0) == "done"


async def test_no_timeout_never_reaches_wait_for(monkeypatch: Any) -> None:
    """Zero overhead when unset: not even a wait_for frame is constructed."""
    wf = _make_context()

    def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("wait_for must not be called when no timeout is set")

    monkeypatch.setattr(asyncio, "wait_for", explode)

    assert await wf.step("plain", lambda: "ok") == "ok"


async def test_step_timeout_cleans_up_the_cancelled_function() -> None:
    from linch.workflow import WorkflowTimeoutError

    wf = _make_context()
    cleaned: list[bool] = []

    async def slow() -> None:
        try:
            await asyncio.sleep(1.0)
        finally:
            cleaned.append(True)

    with pytest.raises(WorkflowTimeoutError):
        await wf.step("slow", slow, timeout_ms=10)

    assert cleaned == [True]
    await asyncio.sleep(0)
    assert _pending_tasks() == []


async def test_step_retry_reruns_fn_and_journals_only_the_winner() -> None:
    from linch.providers.retry import RetryOptions

    seen: list[Any] = []
    wf = _make_context(on_event=seen.append)
    attempts: list[int] = []

    async def flaky() -> str:
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("transient")
        return "ok"

    result = await wf.step(
        "flaky",
        flaky,
        retry=RetryOptions(max_attempts=3, base_delay_ms=0, max_delay_ms=0, jitter=0.0),
    )

    assert result == "ok"
    assert len(attempts) == 3
    ends = [e for e in seen if e.kind == "step_end"]
    assert len(ends) == 1
    assert ends[0].occurrence == 0  # retries must not burn occurrence slots


async def test_step_without_retry_makes_a_single_attempt() -> None:
    wf = _make_context()
    attempts: list[int] = []

    async def always_fails() -> None:
        attempts.append(1)
        raise RuntimeError("transient")

    with pytest.raises(RuntimeError):
        await wf.step("boom", always_fails)

    assert len(attempts) == 1


async def test_step_retry_composes_with_timeout() -> None:
    from linch.providers.retry import RetryOptions

    wf = _make_context()
    attempts: list[int] = []

    async def slow_then_fast() -> str:
        attempts.append(1)
        if len(attempts) == 1:
            await asyncio.sleep(1.0)
        return "ok"

    result = await wf.step(
        "slow",
        slow_then_fast,
        timeout_ms=20,
        retry=RetryOptions(max_attempts=2, base_delay_ms=0, max_delay_ms=0, jitter=0.0),
    )

    assert result == "ok"
    assert len(attempts) == 2


async def test_settled_returns_partial_results_without_cancelling_siblings() -> None:
    wf = _make_context(max_concurrency=8)
    finished: list[str] = []

    def make_ok(name: str):
        async def thunk() -> str:
            await asyncio.sleep(0.02)
            finished.append(name)
            return name

        return thunk

    async def boom() -> str:
        raise RuntimeError("branch failed")

    outcomes = await wf.settled([make_ok("b0"), boom, make_ok("b2")])

    assert sorted(finished) == ["b0", "b2"]  # siblings ran to completion
    assert [o.ok for o in outcomes] == [True, False, True]
    assert [o.value for o in outcomes] == ["b0", None, "b2"]
    assert isinstance(outcomes[1].error, RuntimeError)
    assert outcomes[0].error is None


async def test_settled_propagates_caller_cancellation() -> None:
    wf = _make_context(max_concurrency=8)
    started = asyncio.Event()

    async def branch() -> None:
        started.set()
        await asyncio.sleep(1.0)

    task = asyncio.ensure_future(wf.settled([branch, branch]))
    await asyncio.wait_for(started.wait(), timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await asyncio.sleep(0)
    assert _pending_tasks() == []


async def test_phase_emits_workflow_event() -> None:
    seen: list[Any] = []
    wf = _make_context(on_event=seen.append)

    await wf.phase("Research")

    assert len(seen) == 1
    assert seen[0].type == "workflow"
    assert seen[0].kind == "phase"
    assert seen[0].title == "Research"
