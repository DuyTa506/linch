from __future__ import annotations

from agent_kit.subagents.registry import AgentRegistry
from agent_kit.subagents.types import AgentDefinition, AgentFrontmatter


def _disk_agent(name: str, *, description: str = "Disk agent.") -> AgentDefinition:
    return AgentDefinition(
        name=name,
        file_path=f"/tmp/{name}.md",
        source="disk",
        frontmatter=AgentFrontmatter(
            name=name,
            description=description,
            tools=["Read"],
        ),
        body=f"{name} body",
    )


def test_verification_builtin_is_visible_and_default_is_implicit() -> None:
    registry = AgentRegistry([])

    visible = registry.list()
    names = [agent.name for agent in visible]

    assert "verification" in names
    assert "_default" not in names
    assert registry.get("_default") is not None

    verification = registry.get("verification")
    assert verification is not None
    assert verification.source == "built-in"
    assert verification.frontmatter.tools == ["Read", "Glob", "Grep", "Bash"]


def test_disk_agent_overrides_builtin_verification() -> None:
    disk_verification = _disk_agent(
        "verification",
        description="Project-specific verifier.",
    )
    registry = AgentRegistry([disk_verification])

    resolved = registry.get("verification")
    assert resolved is disk_verification
    assert resolved.source == "disk"

    visible = registry.list()
    assert [agent.name for agent in visible].count("verification") == 1
    assert visible[0] is disk_verification


def test_list_all_includes_default_without_showing_it_in_list() -> None:
    registry = AgentRegistry([_disk_agent("researcher")])

    assert "_default" not in [agent.name for agent in registry.list()]
    assert "_default" in [agent.name for agent in registry.list_all()]

