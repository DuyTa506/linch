"""Parallel search tools with Scheduler V2 resource metadata.

Run:
    python3 examples/parallel_search_agent.py

This example loads ../.env automatically when present. It does not print any
secret values.

Demonstrates:
  1. Read/search tools marked parallel=True.
  2. ResourceAccess declarations for scheduler conflict checks.
  3. Agent(max_tool_concurrency=...) as a hard concurrency limit.
  4. ToolCallEndEvent order matching the original provider tool-call order.

The default demo runs the scheduler directly, so it works without making a
provider call. Use the same tool classes in an Agent for normal LLM-driven runs.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from linch import Agent
from linch.abort import AbortContext
from linch.events import ToolCallEndEvent, ToolCallStartEvent
from linch.permissions import PermissionEngine
from linch.scheduler import execute_tool_calls
from linch.sessions import InMemorySessionStore
from linch.tools import ResourceAccess, ToolContext, ToolRegistry, ToolResult
from linch.types import ToolUseBlock

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


class SearchTool:
    description = "Search a small in-memory source."
    input_schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }
    scope = "read"
    parallel = True
    parallel_safe = True
    tags = ("search", "rag")

    def __init__(self, name: str, source: str, delay_s: float) -> None:
        self.name = name
        self.source = source
        self.delay_s = delay_s

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        query = raw.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        return {"query": query.strip()}

    def resources(self, input: dict[str, Any]) -> list[ResourceAccess]:
        return [ResourceAccess(resource=f"index:{self.source}", mode="read")]

    async def execute(self, input: dict[str, Any], ctx: ToolContext) -> ToolResult:
        await asyncio.sleep(self.delay_s)
        query = input["query"]
        return ToolResult(
            content=f"{self.source}: result for {query!r}",
            summary=f"{self.name}({query})",
            metadata={"source": self.source},
        )

    def summarize(self, input: dict[str, Any]) -> str:
        return f"{self.name}({input.get('query', '?')})"


def build_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.add(SearchTool("SearchDocs", "docs", 0.20))
    registry.add(SearchTool("SearchTickets", "tickets", 0.20))
    registry.add(SearchTool("SearchWiki", "wiki", 0.20))
    return registry


async def run_scheduler_demo() -> None:
    registry = build_registry()
    agent_like = SimpleNamespace(
        cwd=str(ROOT),
        tools=registry,
        permission_engine=PermissionEngine(mode="skip-dangerous"),
        max_tool_concurrency=2,
    )
    session_like = SimpleNamespace(
        id="example",
        store=None,
        active_run_id="scheduler-demo",
        tools_override=None,
        current_turn_allowed_tools=None,
    )
    calls = [
        ToolUseBlock(id="a", name="SearchDocs", input={"query": "scheduler"}),
        ToolUseBlock(id="b", name="SearchTickets", input={"query": "scheduler"}),
        ToolUseBlock(id="c", name="SearchWiki", input={"query": "scheduler"}),
    ]

    print("Running three read/search tools with max_tool_concurrency=2")
    started = time.perf_counter()
    async for event in execute_tool_calls(calls, agent_like, session_like, AbortContext()):
        if isinstance(event, ToolCallStartEvent):
            print(f"  start {event.tool_name}")
        elif isinstance(event, ToolCallEndEvent):
            print(f"  end   {event.tool_name}: {event.result}")
    elapsed = time.perf_counter() - started
    print(f"Elapsed: {elapsed:.2f}s (about two waves, not three serial calls)")


async def build_live_agent() -> Agent:
    load_project_env()
    return Agent(
        model=MODEL,
        openai_api_key=os.environ.get("OPENAI_API_KEY"),
        session_store=InMemorySessionStore(),
        tools=build_registry(),
        max_tool_concurrency=2,
        permissions={"mode": "skip-dangerous"},
        system_prompt=("Use all relevant search tools before answering. Prefer concise answers."),
    )


async def main() -> None:
    await run_scheduler_demo()


if __name__ == "__main__":
    asyncio.run(main())
