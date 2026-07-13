"""File templates rendered by the ``linch`` scaffolding CLI.

Each constant is a ``string.Template``; substitution variables are ``project``
(the kebab-case project name), ``package`` (its snake-case import name), and
``tool`` (a snake-case tool name). Generated bodies mirror the canonical shapes
in docs/usage/quickstart.md so scaffolded projects run and test offline.
"""

from __future__ import annotations

from string import Template

PYPROJECT = Template(
    """\
[build-system]
requires = ["hatchling>=1.24"]
build-backend = "hatchling.build"

[project]
name = "${project}"
version = "0.1.0"
description = "A Linch agent."
requires-python = ">=3.10"
dependencies = ["linch"]

[project.optional-dependencies]
dev = [
  "pytest>=8.0,<9.0",
  "pytest-asyncio>=0.23,<1.0",
]

[tool.hatch.build.targets.wheel]
packages = ["src/${package}"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
pythonpath = ["src"]
"""
)

README = Template(
    """\
# ${project}

A [Linch](https://github.com/DuyTa506/linch) agent scaffolded by `linch new`.

## Install

    pip install -e '.[dev]'

## Run offline (no API key)

    python -m ${package}.agent

Prints a scripted reply so the agent loop is verifiable before any credentials
exist.

## Test

    pytest

Tests run offline: the agent is driven by a scripted provider and the tools are
checked with `linch.testing.assert_tool_contract`.

## Go live

    export OPENAI_API_KEY="..."   # switches to Linch's default OpenAI provider
    export LINCH_MODEL="gpt-5"    # optional; defaults to gpt-5
    python -m ${package}.agent

## Add a tool

    linch add tool <name>

Generates `src/${package}/tools/<name>.py` plus a contract test.
"""
)

GITIGNORE = Template(
    """\
__pycache__/
*.py[cod]
.venv/
.pytest_cache/
dist/
*.egg-info/
.env
"""
)

PKG_INIT = Template('"""${project} — a Linch agent."""\n')

AGENT_MODULE = Template(
    '''\
"""Agent entry point for ${project}.

Runs offline with a scripted provider until OPENAI_API_KEY is set; then
Linch's default OpenAI provider takes over.
"""

import asyncio
import os

from linch import Agent
from linch.config import FeatureFlags
from linch.evals import ScriptedProvider, TextTurn
from linch.sessions import InMemorySessionStore
from linch.tools.registry import empty_tools

from ${package}.tools.greet import greet

OFFLINE_REPLY = "Hello from ${project} (offline scripted reply)."


def build_agent(provider: object | None = None) -> Agent:
    """Build the ${project} agent.

    Args:
        provider: Provider override. When omitted, falls back to a scripted
            offline provider unless OPENAI_API_KEY is set (then Linch's
            default provider is used).

    Returns:
        A configured Agent ready to open sessions.
    """
    if provider is None and not os.environ.get("OPENAI_API_KEY"):
        provider = ScriptedProvider([TextTurn(text=OFFLINE_REPLY)])
    return Agent(
        model=os.environ.get("LINCH_MODEL", "gpt-5"),
        provider=provider,
        tools=empty_tools(greet),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        result_offload=None,
    )


async def main() -> None:
    agent = build_agent()
    session = await agent.session()
    async for event in session.run("Say hello"):
        if event.type == "result":
            print(event.final_text)
    await agent.close()


if __name__ == "__main__":
    asyncio.run(main())
'''
)

TOOLS_INIT = Template('"""Tool package for ${package}."""\n')

GREET_TOOL = Template(
    '''\
"""Example tool: greet a person by name."""

from linch import tool


@tool(description="Greet a person by name.")
async def greet(name: str) -> str:
    return f"Hello, {name}!"
'''
)

TEST_AGENT = Template(
    '''\
"""Offline agent tests — no API key or network required."""

from linch.evals import ScriptedProvider, TextTurn, ToolUseTurn

from ${package}.agent import build_agent


async def _final_text(agent) -> str:
    session = await agent.session()
    final = ""
    async for event in session.run("Say hello"):
        if event.type == "result":
            final = event.final_text
    await agent.close()
    return final


async def test_agent_replies_with_scripted_text():
    agent = build_agent(provider=ScriptedProvider([TextTurn(text="scripted reply")]))
    assert await _final_text(agent) == "scripted reply"


async def test_agent_executes_the_greet_tool():
    agent = build_agent(
        provider=ScriptedProvider(
            [
                ToolUseTurn(tool_name="greet", tool_input={"name": "Linch"}),
                TextTurn(text="done"),
            ]
        )
    )
    assert await _final_text(agent) == "done"
'''
)

TEST_GREET = Template(
    '''\
"""Contract test for the greet tool."""

from linch.testing import assert_tool_contract

from ${package}.tools.greet import greet


async def test_greet_contract():
    result = await assert_tool_contract(
        greet,
        valid_input={"name": "Ada"},
        invalid_input={},
    )
    assert result.content == "Hello, Ada!"
'''
)

TOOL_STUB = Template(
    '''\
"""${tool} tool."""

from linch import tool


@tool(description="TODO: describe ${tool}.")
async def ${tool}(query: str) -> str:
    # TODO: implement ${tool}.
    return f"${tool} results for: {query}"
'''
)

TOOL_TEST_STUB = Template(
    '''\
"""Contract test for the ${tool} tool."""

from linch.testing import assert_tool_contract

from ${package}.tools.${tool} import ${tool}


async def test_${tool}_contract():
    result = await assert_tool_contract(
        ${tool},
        valid_input={"query": "example"},
        invalid_input={},
    )
    assert result.is_error is False
'''
)
