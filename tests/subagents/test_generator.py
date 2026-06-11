from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_generator_schema_requires_nullable_tools() -> None:
    from linch.subagents import generator

    schema = generator._GENERATOR_SCHEMA.schema

    assert "tools" in schema["required"]
    assert schema["properties"]["tools"]["type"] == ["array", "null"]


async def test_rendered_generated_subagent_loads_from_disk(tmp_path) -> None:
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
    assert loaded.agents[0].name == "code-reviewer"
    assert loaded.agents[0].frontmatter.tools == ["Read", "Grep"]


async def test_generate_subagent_definition_from_scripted_provider(tmp_path: Path) -> None:
    from linch import Agent
    from linch.evals import ScriptedProvider, TextTurn
    from linch.sessions import InMemorySessionStore
    from linch.subagents import generate_subagent_definition

    payload = {
        "name": "test-runner",
        "description": "Use when implementation changes need test execution.",
        "body": "You are a test runner. Run relevant tests and report failures.",
        "tools": ["Bash", "Read"],
    }
    agent = Agent(
        model="gpt-5",
        provider=ScriptedProvider([TextTurn(json.dumps(payload))]),
        session_store=InMemorySessionStore(),
        cwd=str(tmp_path),
        permissions={"mode": "skip-dangerous"},
    )

    generated = await generate_subagent_definition(agent, "make a test runner")

    assert generated.name == "test-runner"
    assert generated.description == payload["description"]
    assert generated.body == payload["body"]
    assert generated.tools == ["Bash", "Read"]


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
    from linch import Agent, ConfigError
    from linch.evals import ScriptedProvider, TextTurn
    from linch.sessions import InMemorySessionStore
    from linch.subagents import generate_subagent_definition

    payload = {
        "name": "verification",
        "description": "Use when work needs verification.",
        "body": "You are a verifier.",
        "tools": ["Read"],
    }
    agent = Agent(
        model="gpt-5",
        provider=ScriptedProvider([TextTurn(json.dumps(payload))]),
        session_store=InMemorySessionStore(),
        cwd=str(tmp_path),
        permissions={"mode": "skip-dangerous"},
    )

    with pytest.raises(ConfigError, match="already exists"):
        await generate_subagent_definition(agent, "make a verifier")


async def test_create_subagent_definition_writes_and_reloads(tmp_path: Path) -> None:
    from linch import Agent
    from linch.evals import ScriptedProvider, TextTurn
    from linch.sessions import InMemorySessionStore
    from linch.subagents import create_subagent_definition

    payload = {
        "name": "doc-writer",
        "description": "Use when documentation needs drafting.",
        "body": "You are a documentation writer. Produce concise docs.",
        "tools": [],
    }
    agent = Agent(
        model="gpt-5",
        provider=ScriptedProvider([TextTurn(json.dumps(payload))]),
        session_store=InMemorySessionStore(),
        cwd=str(tmp_path),
        permissions={"mode": "skip-dangerous"},
    )

    created = await create_subagent_definition(agent, "make a documentation writer")

    path = tmp_path / ".linch" / "agents" / "doc-writer.md"
    assert created.file_path == str(path)
    assert path.exists()
    assert agent.subagent_registry is not None
    assert agent.subagent_registry.get("doc-writer") is not None
    tool = agent.tools.get("Subagent")
    assert tool is not None
    assert "- doc-writer:" in tool.description
