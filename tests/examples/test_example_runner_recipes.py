from __future__ import annotations

from pathlib import Path

from tests.support.modules import load_module

_RECIPE = Path(__file__).resolve().parents[2] / "examples" / "recipes" / "runner_recipes.py"


def _load():
    return load_module(_RECIPE, "runner_recipes_example")


async def test_cron_and_webhook_ticks_complete(tmp_path) -> None:
    recipe = _load()
    runner, spec = recipe.build_runner(provider=recipe._AlwaysDoneProvider(), root=str(tmp_path))

    cron = await recipe.cron_tick(runner, spec)
    assert cron.status == "completed"
    assert cron.final_text == "tick complete: queue empty"

    hook = await recipe.webhook_tick(runner, spec, payload="ticket#7")
    assert hook.status == "completed"
    assert hook.iteration > cron.iteration  # durable iteration counter advances
