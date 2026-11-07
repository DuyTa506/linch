from __future__ import annotations

from linch.subagents.registry import AgentRegistry
from linch.subagents.types import AgentDefinition, AgentFrontmatter


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


def test_registry_builtin_and_disk_override() -> None:
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
    assert "_default" in [agent.name for agent in registry.list_all()]
    disk_verification = _disk_agent(
        "verification",
        description="Project-specific verifier.",
    )
    registry = AgentRegistry([disk_verification])

    resolved = registry.get("verification")
    assert resolved is disk_verification
    assert resolved is not None
    assert resolved.source == "disk"

    visible = registry.list()
    assert [agent.name for agent in visible].count("verification") == 1
    assert visible[0] is disk_verification


def test_registry_extra_builtin_overrides_core_builtin_once() -> None:
    from linch.ext.deep_agent.subagents import DEEP_AGENT_SUBAGENTS, VERIFICATION_AGENT

    registry = AgentRegistry([], extra_built_ins=DEEP_AGENT_SUBAGENTS)

    names = [agent.name.lower() for agent in registry.list()]
    assert names.count("verification") == 1
    assert len(names) == len(set(names))
    assert registry.get("verification") is VERIFICATION_AGENT
    assert VERIFICATION_AGENT in registry.list()
