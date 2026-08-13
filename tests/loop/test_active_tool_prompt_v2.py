"""Prompt contributions follow the exact registry selected for a request."""

from __future__ import annotations

from typing import Any


async def test_same_name_session_override_uses_override_prompt_contribution() -> None:
    from linch import Agent, RunOptions
    from linch.config import FeatureFlags, SystemPromptSection
    from linch.evals import ScriptedProvider, TextTurn
    from linch.loop.request import _build_turn_request
    from linch.sessions import InMemorySessionStore
    from linch.tools import ToolContext, ToolRegistry, ToolResult

    class ContributingTool:
        name = "Shared"
        description = "shared"
        input_schema: dict[str, Any] = {"type": "object"}
        scope = "read"
        parallel = False

        def __init__(self, marker: str) -> None:
            self.marker = marker
            self.system_prompt_sections = [
                SystemPromptSection(name=f"section-{marker}", text=f"PROMPT-{marker}")
            ]

        def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
            return raw

        def summarize(self, input: dict[str, Any]) -> str:
            return self.marker

        async def execute(self, input: dict[str, Any], ctx: ToolContext) -> ToolResult:
            return ToolResult(content=self.marker)

    parent = ToolRegistry()
    parent.add(ContributingTool("PARENT"))
    override = ToolRegistry()
    override.add(ContributingTool("OVERRIDE"))
    agent = Agent(
        model="test-model",
        provider=ScriptedProvider([TextTurn("done")]),
        tools=parent,
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        features=FeatureFlags(),
        result_offload=None,
    )
    session = await agent.session()
    session.tools_override = override

    req = _build_turn_request(session, RunOptions())
    rendered = "\n".join(block.text for block in req.system)
    assert "PROMPT-OVERRIDE" in rendered
    assert "PROMPT-PARENT" not in rendered
