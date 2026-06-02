from __future__ import annotations

import pytest


class _FakeProvider:
    id = "fake"

    def context_window(self, model: str) -> int:
        return 128_000

    async def stream(self, req):  # pragma: no cover - these tests do not call the model
        raise AssertionError("provider should not be called")


def _make_agent(*, config_dir: str | None = None):
    from agent_kit import Agent
    from agent_kit.config import FeatureFlags
    from agent_kit.sessions import InMemorySessionStore
    from agent_kit.tools.registry import default_tools

    return Agent(
        model="model-x",
        provider=_FakeProvider(),
        tools=default_tools(),
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        config_dir=config_dir,
        features=FeatureFlags(skills=True, subagents=False, mcp=False),
        result_offload=None,
    )


@pytest.mark.asyncio
async def test_builtin_verify_skill_loads_without_disk_skills() -> None:
    agent = _make_agent()

    await agent.session()

    assert "verify" in agent.skills
    assert agent.tools.get("Skill") is not None
    assert agent.skill_listing_text is not None
    assert "- verify:" in agent.skill_listing_text
    assert "completed work" in agent.skill_listing_text


@pytest.mark.asyncio
async def test_disk_verify_skill_overrides_builtin(tmp_path) -> None:
    skill_dir = tmp_path / "skills" / "verify"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """\
---
description: Project-specific verification workflow.
---
# Project Verify

Run the project-specific checks.
""",
        encoding="utf-8",
    )
    agent = _make_agent(config_dir=str(tmp_path))

    await agent.session()

    verify = agent.skills["verify"]
    assert verify.dir == str(skill_dir)
    assert verify.body.lstrip().startswith("# Project Verify")
    assert agent.skill_listing_text is not None
    assert "Project-specific verification workflow" in agent.skill_listing_text


@pytest.mark.asyncio
async def test_builtin_verify_skill_invocation_records_substituted_body() -> None:
    from agent_kit.tools.base import ToolContext

    agent = _make_agent()
    session = await agent.session()
    tool = agent.tools.get("Skill")
    assert tool is not None

    result = await tool.execute(
        {"skill": "verify", "args": "focus on billing workflow"},
        ToolContext(
            cwd=agent.cwd,
            session_id=session.id,
            run_id="run_1",
            session_store=agent._get_store(),
        ),
    )

    assert result.is_error is False
    assert "Skill base directory: <built-in>/verify" in result.content
    assert "focus on billing workflow" in result.content
    assert "VERDICT: PASS" in result.content
    assert session.invoked_skills[-1].name == "verify"
    assert "focus on billing workflow" in session.invoked_skills[-1].substituted_body

