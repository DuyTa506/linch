from __future__ import annotations

from linch.subagents.registry import AgentRegistry
from linch.tools.subagent import SubagentTool


def test_subagent_tool_description_lists_verification_and_delegation_rules() -> None:
    tool = SubagentTool(
        registry=AgentRegistry([]),
        get_session=lambda _sid: None,
        next_default_display_name=lambda _sid: "Agent #1",
    )

    description = tool.description

    assert "- verification:" in description
    assert "complete context" in description
    assert "meaningful research, implementation, or verification" in description
    assert "based on your findings" in description
    assert "parallel" in description
    assert "_default" not in description

