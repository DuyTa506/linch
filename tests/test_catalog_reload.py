from __future__ import annotations

import threading

import pytest

from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn
from tests.support.tools import StubTool


def _skill_file(root, name: str, description: str) -> None:
    directory = root / ".linch" / "skills" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\ndescription: {description}\n---\n# {name}\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_reload_skills_reuses_skill_tool_and_replaces_derived_rules(tmp_path) -> None:
    from linch.config import FeatureFlags

    _skill_file(tmp_path, "alpha", "The alpha workflow skill.")
    agent = make_agent(
        ScriptedProvider([TextTurn("done")]),
        cwd=str(tmp_path),
        config_dir=".linch",
        features=FeatureFlags(skills=True, subagents=False, mcp=False),
    )
    await agent.connect_skills()

    skill_tool = agent.tools.get("Skill")
    assert skill_tool is not None
    old_revision = agent._catalog_revision
    assert "alpha" in agent.skills
    assert any(getattr(rule, "arg", None) == "alpha" for rule in agent.permission_engine.rules)

    alpha_dir = tmp_path / ".linch" / "skills" / "alpha"
    for child in alpha_dir.iterdir():
        child.unlink()
    alpha_dir.rmdir()
    _skill_file(tmp_path, "beta", "The beta workflow skill.")

    await agent.reload_skills()

    assert agent._catalog_revision == old_revision + 1
    assert "alpha" not in agent.skills
    assert "beta" in agent.skills
    assert agent.tools.get("Skill") is skill_tool
    assert [tool.name for tool in agent.tools.list()].count("Skill") == 1
    assert "beta" in (agent.skill_listing_text or "")
    assert "alpha" not in (agent.skill_listing_text or "")
    assert not any(getattr(rule, "arg", None) == "alpha" for rule in agent.permission_engine.rules)
    assert any(getattr(rule, "arg", None) == "beta" for rule in agent.permission_engine.rules)


@pytest.mark.asyncio
async def test_reload_skills_scans_off_event_loop(tmp_path, monkeypatch) -> None:
    import linch.skills.loader as loader
    from linch.config import FeatureFlags

    _skill_file(tmp_path, "alpha", "The alpha workflow skill.")
    loop_thread = threading.get_ident()
    seen: list[int] = []
    real_loader = loader.load_skills_from_dir

    def spy(config_dir: str, builtin_tool_names: set[str]):
        seen.append(threading.get_ident())
        return real_loader(config_dir, builtin_tool_names)

    monkeypatch.setattr(loader, "load_skills_from_dir", spy)
    agent = make_agent(
        ScriptedProvider([TextTurn("done")]),
        cwd=str(tmp_path),
        config_dir=".linch",
        features=FeatureFlags(skills=True, subagents=False, mcp=False),
    )

    await agent.reload_skills()

    assert seen
    assert all(thread_id != loop_thread for thread_id in seen)


@pytest.mark.asyncio
async def test_reload_skills_keeps_skill_tool_when_catalog_becomes_empty(
    tmp_path, monkeypatch
) -> None:
    import linch.skills.builtins as builtins
    from linch.config import FeatureFlags

    _skill_file(tmp_path, "alpha", "The alpha workflow skill.")
    agent = make_agent(
        ScriptedProvider([TextTurn("done")]),
        cwd=str(tmp_path),
        config_dir=".linch",
        features=FeatureFlags(skills=True, subagents=False, mcp=False),
    )
    await agent.connect_skills()
    skill_tool = agent.tools.get("Skill")
    assert skill_tool is not None

    alpha_dir = tmp_path / ".linch" / "skills" / "alpha"
    for child in alpha_dir.iterdir():
        child.unlink()
    alpha_dir.rmdir()
    monkeypatch.setattr(builtins, "BUILT_IN_SKILLS", [])

    await agent.reload_skills()

    assert agent.skills == {}
    assert agent.tools.get("Skill") is skill_tool
    assert [tool.name for tool in agent.tools.list()].count("Skill") == 1


@pytest.mark.asyncio
async def test_reload_subagents_keeps_feature_off_behavior() -> None:
    from linch.config import FeatureFlags

    agent = make_agent(
        ScriptedProvider([TextTurn("done")]),
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
    )
    agent.tools.register(StubTool("Subagent"))
    agent.tools.register(StubTool("SubagentContinue"))
    agent.tools.register(StubTool("TaskStop"))
    old_revision = agent._catalog_revision

    await agent.reload_subagents()

    assert agent.tools.get("Subagent") is None
    assert agent.tools.get("SubagentContinue") is None
    assert agent.tools.get("TaskStop") is None
    assert agent._catalog_revision == old_revision


@pytest.mark.asyncio
async def test_reload_subagents_replaces_tools_without_moving_their_slots(tmp_path) -> None:
    from linch.config import FeatureFlags

    agents_dir = tmp_path / ".linch" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "research.md").write_text(
        "---\ndescription: Research the repository.\ntools: Read\n---\nResearch.\n",
        encoding="utf-8",
    )
    agent = make_agent(
        ScriptedProvider([TextTurn("done")]),
        cwd=str(tmp_path),
        config_dir=".linch",
        features=FeatureFlags(skills=False, subagents=True, mcp=False),
    )
    await agent.connect_subagents()
    agent.tools.register(StubTool("McpAfterSubagent"))
    before_names = [tool.name for tool in agent.tools.list()]
    before_subagent = agent.tools.get("Subagent")
    old_revision = agent._catalog_revision

    (agents_dir / "research.md").write_text(
        "---\ndescription: Updated research workflow.\ntools: Read\n---\nResearch.\n",
        encoding="utf-8",
    )
    await agent.reload_subagents()

    assert [tool.name for tool in agent.tools.list()] == before_names
    assert agent.tools.get("Subagent") is not before_subagent
    assert agent.subagent_registry is not None
    assert agent.subagent_registry.get("research").frontmatter.description == (
        "Updated research workflow."
    )
    assert agent._catalog_revision == old_revision + 1
