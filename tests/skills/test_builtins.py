from __future__ import annotations

import threading
from typing import Any

import pytest

from linch.config import FeatureFlags
from linch.skills.substitute import substitute_skill_body
from linch.skills.types import Skill, SkillFrontmatter
from linch.tools.registry import workspace_tools
from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider


def _make_agent(config_dir: str | None = None):
    return make_agent(
        ScriptedProvider([]),
        model="model-x",
        tools=workspace_tools(),
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
    agent = _make_agent(str(tmp_path))

    await agent.session()

    verify = agent.skills["verify"]
    assert verify.dir == str(skill_dir)
    assert verify.body.lstrip().startswith("# Project Verify")
    assert agent.skill_listing_text is not None
    assert "Project-specific verification workflow" in agent.skill_listing_text


@pytest.mark.asyncio
async def test_builtin_verify_skill_invocation_records_substituted_body() -> None:
    from linch.tools.base import ToolContext

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


@pytest.mark.asyncio
async def test_skill_discovery_runs_off_the_event_loop(tmp_path, monkeypatch: Any) -> None:
    skill_dir = tmp_path / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\ndescription: A demo skill.\n---\n# Demo\n", encoding="utf-8"
    )

    import linch.skills.loader as loader_mod

    loop_thread = threading.get_ident()
    seen: dict[str, int] = {}
    real = loader_mod.load_skills_from_dir

    def spy(config_dir: str, builtin_tool_names: set[str]):
        seen["thread"] = threading.get_ident()
        return real(config_dir, builtin_tool_names)

    monkeypatch.setattr(loader_mod, "load_skills_from_dir", spy)
    agent = _make_agent(str(tmp_path))
    await agent.session()
    assert "demo" in agent.skills
    assert seen["thread"] != loop_thread


def _skill(body: str, arguments: list[str] | None = None) -> Skill:
    return Skill(
        name="demo",
        dir="/skills/demo",
        frontmatter=SkillFrontmatter(name="demo", description="d", arguments=arguments),
        body=body,
    )


@pytest.mark.parametrize(
    ("body", "arguments", "call_args", "expected"),
    [
        (
            "dir=${LINCH_SKILL_DIR} sid=${LINCH_SESSION_ID}",
            None,
            "",
            ("dir=/skills/demo", "sid=sess-1"),
        ),
        ("keep=${UNKNOWN_VAR}", None, "", ("keep=${UNKNOWN_VAR}",)),
        ("hi $name | all=$ARGUMENTS", ["name"], "alice extra", ("hi alice", "all=alice extra")),
    ],
)
def test_substitute_cases(
    body: str, arguments: list[str] | None, call_args: str, expected: tuple[str, ...]
) -> None:
    out = substitute_skill_body(_skill(body, arguments), call_args, "sess-1")
    for fragment in expected:
        assert fragment in out
