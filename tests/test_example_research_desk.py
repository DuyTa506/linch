"""Smoke test for the non-coding research-desk recipe (ROADMAP Phase 5.5).

Drives `examples/recipes/research_desk.py` with a deterministic ScriptedProvider
(no live key) to prove the recipe wires up: domain tools touch `ctx.deps`, the
closed-loop citation verifier bounces an uncited brief, and a later turn that
records a citation is accepted with a structured Brief.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from linch import RunOptions
from linch.evals import ScriptedProvider, TextTurn, ToolUseTurn

_RECIPE = Path(__file__).resolve().parents[1] / "examples" / "recipes" / "research_desk.py"


def _load_recipe():
    spec = importlib.util.spec_from_file_location("research_desk_example", _RECIPE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_research_desk_verifier_enforces_citation() -> None:
    recipe = _load_recipe()

    provider = ScriptedProvider(
        [
            # First answer is well-formed JSON but cites nothing → verifier retries.
            TextTurn(text=json.dumps({"summary": "Caffeine harms sleep.", "sources": []})),
            # Nudged: read + record a citation.
            ToolUseTurn(
                tool_name="record_citation",
                tool_input={"article_id": "ART-002", "claim": "caffeine cut sleep 41 min"},
            ),
            # Now a grounded brief is accepted.
            TextTurn(
                text=json.dumps(
                    {
                        "summary": "Caffeine within 6h of bed reduced sleep by 41 min.",
                        "sources": ["ART-002"],
                    }
                )
            ),
        ]
    )

    agent, deps = recipe.build_research_desk(provider=provider, model="m")
    session = await agent.session()

    events = [
        event
        async for event in session.run(
            "How does caffeine affect sleep?",
            opts=RunOptions(output_schema=recipe.BRIEF_SCHEMA),
        )
    ]

    result = events[-1]
    assert result.type == "result"
    assert result.subtype == "success"
    assert result.structured_output == {
        "summary": "Caffeine within 6h of bed reduced sleep by 41 min.",
        "sources": ["ART-002"],
    }

    # The verifier bounced the first (uncited) answer.
    retries = [e for e in events if e.type == "verification" and e.action == "retry"]
    assert len(retries) == 1
    assert retries[0].verifier == "cited-sources"

    # The citation ledger in deps was grounded by the write tool.
    assert deps["citations"] == [{"article_id": "ART-002", "claim": "caffeine cut sleep 41 min"}]
