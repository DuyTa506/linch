from __future__ import annotations

import json
from pathlib import Path

from linch import RunOptions
from tests.support.modules import load_module
from tests.support.providers import ScriptedProvider, TextTurn, ToolUseTurn

_RECIPE = Path(__file__).resolve().parents[2] / "examples" / "recipes" / "research_desk.py"


def _load_recipe():
    return load_module(_RECIPE, "research_desk_example")


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
