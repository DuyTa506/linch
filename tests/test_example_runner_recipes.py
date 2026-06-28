"""Smoke test for the host-owned runner recipes (examples/recipes/runner_recipes.py).

Drives each recipe (cron, webhook, fixed-interval, CI gate) with the example's
own fake provider — no live key — proving they are thin wrappers over
``LoopRunner.run_once()`` and that the host owns the lifecycle.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_RECIPE = Path(__file__).resolve().parents[1] / "examples" / "recipes" / "runner_recipes.py"


def _load():
    spec = importlib.util.spec_from_file_location("runner_recipes_example", _RECIPE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_cron_and_webhook_ticks_complete(tmp_path) -> None:
    recipe = _load()
    runner, spec = recipe.build_runner(provider=recipe._AlwaysDoneProvider(), root=str(tmp_path))

    cron = await recipe.cron_tick(runner, spec)
    assert cron.status == "completed"
    assert cron.final_text == "tick complete: queue empty"

    hook = await recipe.webhook_tick(runner, spec, payload="ticket#7")
    assert hook.status == "completed"
    assert hook.iteration > cron.iteration  # durable iteration counter advances


async def test_fixed_interval_runs_each_tick(tmp_path) -> None:
    recipe = _load()
    runner, spec = recipe.build_runner(provider=recipe._AlwaysDoneProvider(), root=str(tmp_path))

    results = await recipe.fixed_interval(runner, spec, ticks=3, interval_s=0.0)

    assert len(results) == 3
    assert all(r.status == "completed" for r in results)
    # Iterations are strictly increasing across ticks.
    assert [r.iteration for r in results] == sorted({r.iteration for r in results})


async def test_ci_gate_returns_zero_on_success(tmp_path) -> None:
    recipe = _load()
    runner, spec = recipe.build_runner(provider=recipe._AlwaysDoneProvider(), root=str(tmp_path))

    assert await recipe.ci_gate(runner, spec) == 0
