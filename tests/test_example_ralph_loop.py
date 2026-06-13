"""Smoke test for the Ralph-loop recipe (examples/recipes/ralph_loop.py).

Drives the loop with a deterministic ScriptedProvider (no live key): three passes,
each in a fresh session, each writing progress to the virtual filesystem. The loop's
done-predicate reads that same backend and stops when the DONE marker appears — proving
the recipe carries state across fresh contexts via the filesystem alone.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from linch.evals import ScriptedProvider, TextTurn, ToolUseTurn

_RECIPE = Path(__file__).resolve().parents[1] / "examples" / "recipes" / "ralph_loop.py"


def _load():
    spec = importlib.util.spec_from_file_location("ralph_loop_example", _RECIPE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(content: str) -> ToolUseTurn:
    return ToolUseTurn(
        tool_name="write_file", tool_input={"path": "/progress.md", "content": content}
    )


async def test_ralph_loop_converges_across_fresh_contexts() -> None:
    recipe = _load()

    # Three iterations: each does one item and rewrites /progress.md; the third
    # appends the DONE marker so the loop's predicate stops it.
    provider = ScriptedProvider(
        [
            _write("[x] scaffold\n[ ] implement\n[ ] test"),
            TextTurn(text="did scaffold"),
            _write("[x] scaffold\n[x] implement\n[ ] test"),
            TextTurn(text="did implement"),
            _write(f"[x] scaffold\n[x] implement\n[x] test\n{recipe.DONE_MARKER}"),
            TextTurn(text="did test — done"),
        ]
    )
    agent, backend = recipe.build_ralph_agent(provider=provider, model="m")

    seen: list[int] = []
    result = await recipe.run_ralph_loop(
        agent, backend, max_iterations=8, on_iteration=lambda i, _t: seen.append(i)
    )

    # Stopped exactly when the marker appeared — not at max_iterations.
    assert result == {"iterations": 3, "done": True}
    assert seen == [1, 2, 3]

    # The filesystem (the only carried memory) holds the converged state.
    final = await backend.read("/progress.md")
    assert recipe.DONE_MARKER in final
    assert final.count("[x]") == 3


async def test_ralph_loop_is_bounded_when_it_never_converges() -> None:
    recipe = _load()

    # Every pass writes the same un-finished progress → predicate never true.
    provider = ScriptedProvider([_write("[ ] scaffold"), TextTurn(text="stuck")] * 3)
    agent, backend = recipe.build_ralph_agent(provider=provider, model="m")

    result = await recipe.run_ralph_loop(agent, backend, max_iterations=3)

    # max_iterations is the fallibility backstop — the loop gives up, doesn't hang.
    assert result == {"iterations": 3, "done": False}
