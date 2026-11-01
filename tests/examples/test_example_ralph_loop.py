from __future__ import annotations

from pathlib import Path

from tests.support.modules import load_module
from tests.support.providers import ScriptedProvider, TextTurn, ToolUseTurn

_RECIPE = Path(__file__).resolve().parents[2] / "examples" / "recipes" / "ralph_loop.py"


def _load():
    return load_module(_RECIPE, "ralph_loop_example")


def _write(content: str) -> ToolUseTurn:
    return ToolUseTurn(
        tool_name="write_file", tool_input={"path": "/progress.md", "content": content}
    )


def _enable_ralph_filesystem(agent) -> None:
    """Opt the recipe's neutral Agent into its virtual filesystem preset."""
    from linch.filesystem import filesystem_tools

    agent.features.filesystem = True
    for tool in filesystem_tools():
        agent.tools.register(tool)


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
    _enable_ralph_filesystem(agent)

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
