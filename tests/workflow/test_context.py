"""WorkflowContext combinator tests (no provider needed).

linch imports happen inside test functions because tests/loop/test_hardening.py
pops all ``linch*`` modules from ``sys.modules``.
"""

from __future__ import annotations

import asyncio
from typing import Any


def _make_context(**kwargs: Any) -> Any:
    from linch.workflow import WorkflowContext

    return WorkflowContext(agent=None, host_session=None, **kwargs)


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


async def test_phase_emits_workflow_event() -> None:
    seen: list[Any] = []
    wf = _make_context(on_event=seen.append)

    await wf.phase("Research")

    assert len(seen) == 1
    assert seen[0].type == "workflow"
    assert seen[0].kind == "phase"
    assert seen[0].title == "Research"
