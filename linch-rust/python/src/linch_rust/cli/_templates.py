"""The files `linch-rust new` and `linch-rust add tool` write.

Each is a `string.Template` over `project` (the kebab-case project name),
`package` (its snake-case import name) and `tool` (a snake-case tool name).
They follow Python Linch's scaffold, on this client: the agent is a config
opened on a `linch-server` sidecar, and runs offline on the scripted
provider.
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
description = "An agent on the Linch Rust core."
requires-python = ">=3.11"
dependencies = ["linch-rust"]

[project.optional-dependencies]
dev = [
  "pytest>=8.3",
  "pytest-asyncio>=0.25",
]

[tool.hatch.build.targets.wheel]
packages = ["src/${package}"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
testpaths = ["tests"]
pythonpath = ["src"]
"""
)

README = Template(
    """\
# ${project}

An agent on the Linch Rust core, scaffolded by `linch-rust new`. The
`linch-rust` package runs it on its bundled `linch-server` sidecar.

## Install

    pip install -e '.[dev]'

## Run offline (no API key)

    python -m ${package}.agent

The scripted provider answers, so the agent loop works before any key
exists.

## Test

    pytest

The tests run offline too: each scripts the model's turns.

## Go live

    export OPENAI_API_KEY="..."   # switches to OpenAI's Responses API
    export LINCH_MODEL="gpt-5"    # optional; gpt-5 when unset
    python -m ${package}.agent

The sidecar reads the key from its environment; the config never carries
it. Edit `build_config` in `src/${package}/agent.py` for another provider.

## Add a tool

    linch-rust add tool <name>

It writes `src/${package}/tools/<name>.py` and its test. Add the tool to
`TOOLS` in `src/${package}/agent.py` to give it to the agent.
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

PACKAGE_INIT = Template('"""${project}: an agent on the Linch Rust core."""\n')

AGENT_MODULE = Template(
    '''\
"""Agent entry point for ${project}.

It runs offline on the scripted provider until OPENAI_API_KEY is set; then
it uses OpenAI's Responses API.
"""

import asyncio
import os
from typing import Any

from linch_rust import AgentConfig, ResultEvent, Server
from linch_rust.scripted import MODEL, text_turn

from ${package}.tools.greet import greet

OFFLINE_REPLY = "Hello from ${project} (offline scripted reply)."
TOOLS = [greet]


def build_config(turns: list[dict[str, Any]] | None = None) -> AgentConfig:
    """Build the ${project} agent config.

    Args:
        turns: Scripted provider turns to replay instead of a live model.
            When absent, the agent is live if OPENAI_API_KEY is set, and
            answers OFFLINE_REPLY otherwise.

    Returns:
        The config `Server.open_agent` opens.
    """
    if turns is None and os.environ.get("OPENAI_API_KEY"):
        model = os.environ.get("LINCH_MODEL", "gpt-5")
        provider: dict[str, Any] = {"id": "openai-responses"}
    else:
        model = MODEL
        scripted = [text_turn(OFFLINE_REPLY)] if turns is None else turns
        provider = {"id": "scripted", "turns": scripted}
    return {"namespace": "${project}", "spec": {"model": model}, "provider": provider}


async def ask(prompt: str, config: AgentConfig | None = None) -> str:
    """Run `prompt` once on a new sidecar and return the final text.

    Args:
        prompt: The user's text.
        config: The agent config; `build_config()` when absent.

    Returns:
        The run's final text, empty when it ended without one.
    """
    if config is None:
        config = build_config()
    async with Server() as server:
        agent = await server.open_agent(config, tools=TOOLS)
        run = await agent.run("main", prompt)
        result = await run.result()
    if isinstance(result, ResultEvent):
        return result.final_text or ""
    return ""


async def main() -> None:
    print(await ask("Say hello"))


if __name__ == "__main__":
    asyncio.run(main())
'''
)

TOOLS_INIT = Template('"""Host tools of ${package}."""\n')

GREET_TOOL = Template(
    '''\
"""Example host tool: greet a person by name."""

from linch_rust import tool


@tool(scope="read")
async def greet(name: str) -> str:
    """Greet a person by name."""
    return f"Hello, {name}!"
'''
)

TEST_AGENT = Template(
    '''\
"""Offline agent tests: the model is scripted, so no key or network is needed."""

from linch_rust.scripted import text_turn, tool_turn

from ${package}.agent import OFFLINE_REPLY, ask, build_config


async def test_agent_replies_with_scripted_text():
    reply = await ask("Say hello", build_config([text_turn("scripted reply")]))
    assert reply == "scripted reply"


async def test_agent_runs_offline_without_a_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert await ask("Say hello") == OFFLINE_REPLY


async def test_agent_executes_the_greet_tool():
    turns = [tool_turn("call_1", "greet", {"name": "Linch"}), text_turn("done")]
    assert await ask("Greet Linch", build_config(turns)) == "done"
'''
)

TEST_GREET = Template(
    '''\
"""Tests of the greet tool: its declaration and its answer."""

from ${package}.tools.greet import greet


def test_greet_declares_its_input():
    spec = greet.spec()
    assert spec["scope"] == "read"
    assert spec["input_schema"]["required"] == ["name"]


async def test_greet_answers():
    assert await greet.fn(name="Ada") == "Hello, Ada!"
'''
)

TOOL_STUB = Template(
    '''\
"""The ${tool} host tool."""

from linch_rust import tool


@tool(scope="read")
async def ${tool}(query: str) -> str:
    """TODO: describe ${tool}; the model reads this."""
    # TODO: implement ${tool}.
    return f"${tool} results for: {query}"
'''
)

TOOL_TEST_STUB = Template(
    '''\
"""Tests of the ${tool} tool."""

from ${package}.tools.${tool} import ${tool}


def test_${tool}_declares_its_input():
    spec = ${tool}.spec()
    assert spec["name"] == "${tool}"
    assert spec["input_schema"]["required"] == ["query"]


async def test_${tool}_answers():
    assert await ${tool}.fn(query="example") == "${tool} results for: example"
'''
)
