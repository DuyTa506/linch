from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn


def _make_agent(provider, tmp_path):
    from linch import workspace_tools
    from linch.config import FeatureFlags

    return make_agent(
        provider,
        model="gpt-5",
        tools=workspace_tools(),
        cwd=str(tmp_path),
        features=FeatureFlags(subagents=True),
    )


def test_generator_schema_requires_nullable_tools() -> None:
    from linch.subagents import generator

    schema = generator._GENERATOR_SCHEMA.schema
    assert "tools" in schema["required"]
    assert schema["properties"]["tools"]["type"] == ["array", "null"]


async def test_rendered_generated_subagent_loads_from_disk(tmp_path: Path) -> None:
    from linch.subagents import GeneratedSubagentDefinition, load_agents_from_dir
    from linch.subagents.generator import render_subagent_markdown

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "code-reviewer.md").write_text(
        render_subagent_markdown(
            GeneratedSubagentDefinition(
                name="Code Reviewer",
                description="Use when code changes need focused review.",
                body="You are a focused code review subagent.",
                tools=["Read", "Grep"],
            )
        ),
        encoding="utf-8",
    )
    loaded = await load_agents_from_dir(str(tmp_path))
    assert loaded.skipped == []
    assert len(loaded.agents) == 1
    parsed = loaded.agents[0]
    assert parsed.name == "code-reviewer"
    assert parsed.source == "disk"
    assert parsed.frontmatter.tools == ["Read", "Grep"]


async def test_generate_subagent_definition_from_scripted_provider(tmp_path: Path) -> None:
    from linch.subagents import generate_subagent_definition

    payload = {
        "name": "test-runner",
        "description": "Use when implementation changes need test execution.",
        "body": "You are a test runner. Run relevant tests and report failures.",
        "tools": ["Bash", "Read"],
    }
    generated = await generate_subagent_definition(
        _make_agent(ScriptedProvider([TextTurn(json.dumps(payload))]), tmp_path),
        "make a test runner",
    )
    assert generated.name == "test-runner"
    assert generated.description == payload["description"]
    assert generated.body == payload["body"]
    assert generated.tools == payload["tools"]

    for field in ("omitted", None):
        null_payload = {k: v for k, v in payload.items() if k != "tools"}
        if field is None:
            null_payload["tools"] = None
        generated = await generate_subagent_definition(
            _make_agent(ScriptedProvider([TextTurn(json.dumps(null_payload))]), tmp_path),
            "make an unrestricted runner",
        )
        assert generated.tools is None


async def test_write_subagent_definition_rejects_existing_file(tmp_path: Path) -> None:
    from linch import ConfigError
    from linch.subagents import GeneratedSubagentDefinition, write_subagent_definition

    definition = GeneratedSubagentDefinition(
        name="reviewer",
        description="Use when code needs review.",
        body="You are a reviewer.",
    )

    await write_subagent_definition(definition, tmp_path)
    with pytest.raises(ConfigError, match="already exists"):
        await write_subagent_definition(definition, tmp_path)


async def test_generate_subagent_definition_rejects_existing_name(tmp_path: Path) -> None:
    from linch import ConfigError
    from linch.subagents import generate_subagent_definition

    payload = {
        "name": "verification",
        "description": "Use when work needs verification.",
        "body": "You are a verifier.",
        "tools": ["Read"],
    }
    agent = _make_agent(ScriptedProvider([TextTurn(json.dumps(payload))]), tmp_path)

    with pytest.raises(ConfigError, match="already exists"):
        await generate_subagent_definition(agent, "make a verifier")


async def test_create_subagent_definition_writes_and_reloads(tmp_path: Path) -> None:
    from linch.subagents import create_subagent_definition

    payload = {
        "name": "doc-writer",
        "description": "Use when documentation needs drafting.",
        "body": "You are a documentation writer. Produce concise docs.",
        "tools": ["Read"],
    }
    agent = _make_agent(ScriptedProvider([TextTurn(json.dumps(payload))]), tmp_path)

    created = await create_subagent_definition(agent, "make a documentation writer")

    path = tmp_path / ".linch" / "agents" / "doc-writer.md"
    assert created.file_path == str(path)
    assert created.definition.description == payload["description"]
    assert created.definition.body == payload["body"]
    assert created.definition.tools == payload["tools"]
    assert path.exists()
    assert agent.subagent_registry is not None
    assert agent.subagent_registry.get("doc-writer") is not None
    tool = agent.tools.get("Subagent")
    assert tool is not None
    assert "- doc-writer:" in tool.description
