"""Custom tools — five patterns.

Run:
    OPENAI_API_KEY=sk-... python examples/01_custom_tools.py

Demonstrates:
  1. Read-only tool           — parallel_safe=True, no side effects
  2. Write tool               — parallel_safe=False, mutates state
  3. Exec tool                — scope="exec", runs external processes
  4. Tool with validation     — raises ValueError on bad input
  5. Tool using ctx.deps      — shares a Python object across all calls

Each pattern is wired into a standalone agent so you can pick the snippet
you need and paste it into your own project.
"""

from __future__ import annotations

import asyncio
import os

from agent_kit import Agent
from agent_kit.config import FeatureFlags, SystemPromptConfig
from agent_kit.sessions import InMemorySessionStore
from agent_kit.tools.base import ToolContext, ToolResult
from agent_kit.tools.registry import empty_tools

API_KEY = os.environ.get("OPENAI_API_KEY", "")
MODEL = "gpt-5-nano-2025-08-07"


# ── Pattern 1: Read-only, parallel-safe tool ─────────────────────────────────
#
# Good for: web search, KB lookup, database SELECT, API fetch.
# parallel_safe=True means AgentKit will run this concurrently with other
# parallel-safe tools in the same turn — no lock needed.


class WeatherTool:
    name = "get_weather"
    description = "Return the current weather for a city."
    input_schema = {
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "City name, e.g. 'Tokyo'"}
        },
        "required": ["city"],
    }
    scope = "read"
    parallel_safe = True  # safe to run concurrently

    def validate(self, raw: dict) -> dict:
        if not raw.get("city"):
            raise ValueError("city is required")
        return {"city": str(raw["city"])}

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        # In production, call a real weather API here.
        # ctx.signal is an AbortContext — check it for cancellation in long calls.
        city = input["city"]
        fake_data = {"Tokyo": "22°C, partly cloudy", "Paris": "18°C, sunny"}
        result = fake_data.get(city, f"No data for {city}")
        return ToolResult(content=result, summary=f"weather({city})")

    def summarize(self, input: dict) -> str:
        return f"get_weather({input.get('city', '?')})"


# ── Pattern 2: Write tool (mutates state) ────────────────────────────────────
#
# Good for: saving to DB, updating a file, posting to an API.
# parallel_safe=False ensures this runs serially — never concurrently.


class SaveNoteTool:
    name = "save_note"
    description = "Save a note to the in-memory notebook."
    input_schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["title", "content"],
    }
    scope = "write"
    parallel_safe = False  # writes must be serial

    def __init__(self, notebook: dict[str, str]) -> None:
        # State lives in the passed-in dict — shared across all calls.
        self._notebook = notebook

    def validate(self, raw: dict) -> dict:
        if not raw.get("title"):
            raise ValueError("title is required")
        return raw

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        self._notebook[input["title"]] = input["content"]
        return ToolResult(
            content=f"Saved '{input['title']}'.",
            summary=f"save_note({input['title']})",
        )

    def summarize(self, input: dict) -> str:
        return f"save_note({input.get('title', '?')})"


# ── Pattern 3: Exec tool ────────────────────────────────────────────────────
#
# Good for: running CLI commands, spawning sub-processes.
# scope="exec" signals to the permission engine that this is dangerous.
# Pair with a permission rule or canUseTool callback in production.


class RunCommandTool:
    name = "run_command"
    description = "Run a whitelisted shell command and return its output."
    input_schema = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "enum": ["date", "hostname", "uptime"],
                "description": "Whitelisted command to run.",
            }
        },
        "required": ["command"],
    }
    scope = "exec"
    parallel_safe = False

    def validate(self, raw: dict) -> dict:
        allowed = {"date", "hostname", "uptime"}
        if raw.get("command") not in allowed:
            raise ValueError(f"command must be one of {allowed}")
        return raw

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        import subprocess

        result = subprocess.run(
            input["command"], shell=True, capture_output=True, text=True, timeout=5
        )
        output = result.stdout.strip() or result.stderr.strip() or "(no output)"
        return ToolResult(content=output, summary=f"run({input['command']})")

    def summarize(self, input: dict) -> str:
        return f"run_command({input.get('command', '?')})"


# ── Pattern 4: Tool with ctx.deps ───────────────────────────────────────────
#
# Good for: when many tools need the same client (DB connection, vector store).
# Pass deps=... to Agent, access via ctx.deps in execute().
# This avoids __init__ closures and makes per-run swapping easy.


class SearchKbTool:
    name = "search_kb"
    description = "Search the knowledge base using the provided query."
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "top_k": {"type": "integer", "default": 3, "minimum": 1, "maximum": 10},
        },
        "required": ["query"],
    }
    scope = "read"
    parallel_safe = True

    def validate(self, raw: dict) -> dict:
        if not raw.get("query"):
            raise ValueError("query is required")
        return {"query": str(raw["query"]), "top_k": int(raw.get("top_k", 3))}

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        # ctx.deps is whatever was passed as Agent(deps=...) or RunOptions(deps=...)
        kb = ctx.deps  # expected to be a dict or object with .search()
        if isinstance(kb, dict):
            # Simple dict KB: check if any key word appears in the query
            hits = [v for k, v in kb.items() if k.lower() in input["query"].lower()]
            content = "\n".join(hits[: input["top_k"]]) or "No results."
        else:
            content = await kb.search(input["query"], top_k=input["top_k"])
        return ToolResult(
            content=content,
            summary=f"search_kb({input['query'][:40]})",
        )

    def summarize(self, input: dict) -> str:
        return f"search_kb({input.get('query', '?')[:40]})"


# ── Pattern 5: Validation-heavy tool ────────────────────────────────────────
#
# validate() is called BEFORE execute(). If it raises, the tool returns an
# immediate error result without hitting execute() at all.
# Use this for input sanitisation, type coercion, and range checks.


class CalculatorTool:
    name = "calculate"
    description = "Evaluate a simple arithmetic expression."
    input_schema = {
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "Expression containing only numbers and + - * / ( ).",
            }
        },
        "required": ["expression"],
    }
    scope = "read"
    parallel_safe = True

    _SAFE_CHARS = set("0123456789 +-*/(). ")

    def validate(self, raw: dict) -> dict:
        expr = raw.get("expression", "")
        if not isinstance(expr, str) or not expr.strip():
            raise ValueError("expression must be a non-empty string")
        if not all(c in self._SAFE_CHARS for c in expr):
            raise ValueError("expression contains unsafe characters")
        return {"expression": expr.strip()}

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        try:
            result = eval(input["expression"], {"__builtins__": {}})  # noqa: S307
            return ToolResult(
                content=str(result),
                summary=f"calc({input['expression']}) = {result}",
            )
        except Exception as exc:
            return ToolResult(
                content=f"Error: {exc}",
                summary="calc(error)",
                is_error=True,
            )

    def summarize(self, input: dict) -> str:
        return f"calculate({input.get('expression', '?')})"


# ── Demos ─────────────────────────────────────────────────────────────────────


async def demo_read_tool() -> None:
    print("\n── Demo 1: Read-only weather tool ──")
    agent = Agent(
        model=MODEL,
        openai_api_key=API_KEY,
        system_prompt_config=SystemPromptConfig(
            replace_defaults=True,
            append="You are a weather assistant. Always call get_weather before answering.",
        ),
        tools=empty_tools(WeatherTool()),
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
    )
    session = await agent.session()
    async for event in session.run("What's the weather in Tokyo and Paris?"):
        if event.type == "result":
            print("Answer:", event.final_text)


async def demo_write_tool() -> None:
    print("\n── Demo 2: Write tool (notebook) ──")
    notebook: dict[str, str] = {}
    agent = Agent(
        model=MODEL,
        openai_api_key=API_KEY,
        system_prompt_config=SystemPromptConfig(
            replace_defaults=True,
            append="You are a note-taking assistant. Use save_note to persist information.",
        ),
        tools=empty_tools(SaveNoteTool(notebook)),
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
    )
    session = await agent.session()
    msg = "Save a note titled 'Shopping list' with items: milk, eggs, bread."
    async for event in session.run(msg):
        if event.type == "result":
            print("Answer:", event.final_text)
    print("Notebook contents:", notebook)


async def demo_deps_tool() -> None:
    print("\n── Demo 3: Tool using ctx.deps ──")
    # Fake knowledge base — in production this would be a vector store client
    kb = {
        "pricing": "Basic plan: $10/mo. Pro plan: $50/mo. Enterprise: contact sales.",
        "support": "Support is available 24/7 via chat and email.",
        "trial": "We offer a 14-day free trial with no credit card required.",
    }
    agent = Agent(
        model=MODEL,
        openai_api_key=API_KEY,
        system_prompt_config=SystemPromptConfig(
            replace_defaults=True,
            append="You are a product assistant. Use search_kb to look up info.",
        ),
        tools=empty_tools(SearchKbTool()),
        deps=kb,  # available as ctx.deps inside SearchKbTool.execute
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
    )
    session = await agent.session()
    async for event in session.run("Do you offer a free trial?"):
        if event.type == "result":
            print("Answer:", event.final_text)


async def demo_calculator() -> None:
    print("\n── Demo 4: Validation-heavy calculator tool ──")
    agent = Agent(
        model=MODEL,
        openai_api_key=API_KEY,
        system_prompt_config=SystemPromptConfig(
            replace_defaults=True,
            append="You are a math assistant. Use the calculate tool for all arithmetic.",
        ),
        tools=empty_tools(CalculatorTool()),
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
    )
    session = await agent.session()
    async for event in session.run("What is (123 * 456) + (789 / 3)?"):
        if event.type == "result":
            print("Answer:", event.final_text)


async def main() -> None:
    if not API_KEY:
        print("Set OPENAI_API_KEY to run this example.")
        return
    await demo_read_tool()
    await demo_write_tool()
    await demo_deps_tool()
    await demo_calculator()


if __name__ == "__main__":
    asyncio.run(main())
