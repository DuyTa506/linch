"""Runtime-created tools and registry selection.

Run:
    python3 examples/runtime_tools.py

This example loads ../.env automatically when present. It does not print any
secret values.

Demonstrates:
  1. ToolRegistry.add/remove/replace.
  2. ToolRegistry.select by tool name and tag.
  3. Provider schema export through registry.schemas().
  4. Runtime tools that are read-scoped and parallel-safe.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from linch import Agent
from linch.sessions import InMemorySessionStore
from linch.tools import ResourceAccess, ToolContext, ToolRegistry, ToolResult

ROOT = Path(__file__).resolve().parents[1]
MODEL = "gpt-5-nano-2025-08-07"


def load_project_env() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


class RuntimeLookupTool:
    description = "Lookup one key from a runtime-provided dictionary."
    input_schema = {
        "type": "object",
        "properties": {"key": {"type": "string"}},
        "required": ["key"],
    }
    scope = "read"
    parallel = True
    parallel_safe = True
    tags = ("runtime", "lookup")
    capabilities = ("kv.lookup",)
    cost_hint = "local"

    def __init__(self, name: str, data: dict[str, str]) -> None:
        self.name = name
        self._data = data

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        key = raw.get("key")
        if not isinstance(key, str) or not key.strip():
            raise ValueError("key must be a non-empty string")
        return {"key": key.strip()}

    def resources(self, input: dict[str, Any]) -> list[ResourceAccess]:
        return [ResourceAccess(resource=f"kv:{self.name}", mode="read")]

    async def execute(self, input: dict[str, Any], ctx: ToolContext) -> ToolResult:
        key = input["key"]
        value = self._data.get(key, "not found")
        return ToolResult(
            content=value,
            summary=f"{self.name}({key})",
            metadata={"key": key},
        )

    def summarize(self, input: dict[str, Any]) -> str:
        return f"{self.name}({input.get('key', '?')})"


def registry_demo() -> ToolRegistry:
    registry = ToolRegistry()

    registry.add(RuntimeLookupTool("LookupProduct", {"plan": "Pro", "sla": "99.9%"}))
    registry.add(RuntimeLookupTool("LookupPolicy", {"refund": "30 days"}))
    print("After add:", [tool.name for tool in registry.list()])

    registry.replace(RuntimeLookupTool("LookupPolicy", {"refund": "45 days"}))
    print("After replace LookupPolicy:", registry.get("LookupPolicy") is not None)

    removed = registry.remove("LookupProduct")
    print("Removed:", removed.name if removed else None)
    registry.add(RuntimeLookupTool("LookupProduct", {"plan": "Enterprise"}))

    selected = registry.select(names={"LookupPolicy"}, tags={"lookup"})
    print("Selected:", [tool.name for tool in selected.list()])

    print("Provider schemas:")
    for schema in registry.schemas():
        print(f"  - {schema['name']}: {schema['input_schema']['required']}")

    return registry


async def maybe_live_agent(registry: ToolRegistry) -> None:
    load_project_env()
    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY not set; skipped live agent call.")
        return

    agent = Agent(
        model=MODEL,
        openai_api_key=os.environ.get("OPENAI_API_KEY"),
        tools=registry,
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        system_prompt="Use the runtime lookup tools to answer.",
    )
    session = await agent.session()
    async for event in session.run("What is the refund policy and product plan?"):
        if event.type == "result":
            print("Live answer:", event.final_text)


async def main() -> None:
    registry = registry_demo()
    await maybe_live_agent(registry)


if __name__ == "__main__":
    asyncio.run(main())
