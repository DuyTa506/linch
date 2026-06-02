"""loop_guard_agent.py — demonstrate the loop guard catching a repeated-call loop.

Run with:
    python examples/loop_guard_agent.py

The script uses a fake provider that always calls the same tool with the same
input.  Without a loop guard the agent would loop until max_turns is hit.
With the default LoopGuard (max_identical_tool_calls=3) the guard trips cleanly
on the 3rd identical call and emits a LoopGuardEvent before stopping.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

# Load .env from the project root when present (never print secret values).
_env_path = Path(__file__).parent.parent / ".env"
if _env_path.is_file():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())


# ── Fake provider that loops forever (same tool call, every turn) ──────────


class LoopingProvider:
    """Fake provider: always emits the same SearchDocs tool call."""

    id = "looping-demo"

    def context_window(self, model: str) -> int:
        return 128_000

    async def stream(self, req):
        from linch.types import Usage

        # If tools were stripped (force_final turn), return a text response.
        if not req.tools:
            yield {"type": "message_start", "model": req.model}
            yield {"type": "text_delta", "text": "I have been unable to proceed."}
            yield {
                "type": "message_end",
                "stop_reason": "end_turn",
                "usage": Usage(),
                "provider_metadata": None,
            }
            return

        tool_id = "search_001"
        yield {"type": "message_start", "model": req.model}
        yield {"type": "tool_use_start", "id": tool_id, "name": "SearchDocs"}
        yield {
            "type": "tool_use_input_delta",
            "id": tool_id,
            "json_delta": json.dumps({"query": "how to fix the bug"}),
        }
        yield {"type": "tool_use_end", "id": tool_id}
        yield {
            "type": "message_end",
            "stop_reason": "tool_use",
            "usage": Usage(),
            "provider_metadata": None,
        }


# ── Fake tool ──────────────────────────────────────────────────────────────


class SearchDocsTool:
    name = "SearchDocs"
    description = "Search the documentation."
    input_schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }
    scope = "read"
    parallel = True
    tags = ("search",)

    def validate(self, raw):
        return raw

    def summarize(self, input):
        return f"search({input.get('query', '')})"

    def resources(self, input):
        return []

    async def execute(self, input, ctx):
        from linch.tools import ToolResult

        return ToolResult(content="No results found.", summary="SearchDocs")


# ── Main ───────────────────────────────────────────────────────────────────


async def main() -> None:
    from linch import Agent, LoopGuard
    from linch.config import FeatureFlags
    from linch.events import ErrorEvent, LoopGuardEvent, ResultEvent
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools

    print("=== Loop Guard Demo ===\n")

    # ── Demo 1: default guard (stop on 3rd identical call) ─────────────
    print("Demo 1: Default LoopGuard — clean stop after 3 identical calls\n")

    agent = Agent(
        model="demo-model",
        provider=LoopingProvider(),
        tools=empty_tools(SearchDocsTool()),
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        # loop_guard uses default LoopGuard(max_identical_tool_calls=3)
    )

    session = await agent.session()
    call_count = 0
    async for event in session.run("find a solution to my bug"):
        if isinstance(event, LoopGuardEvent):
            print(f"  ✓ LoopGuardEvent: action={event.action!r}")
            print(f"    reason : {event.reason}")
            print(f"    detail : {event.detail}")
        elif isinstance(event, ResultEvent):
            symbol = "✓" if event.subtype == "success" else "✗"
            print(f"  {symbol} ResultEvent: subtype={event.subtype!r}")
        elif event.type == "tool_call_start":  # type: ignore[union-attr]
            call_count += 1
            print(f"  [tool call #{call_count}] {event.tool_name}")  # type: ignore[union-attr]

    print(f"\n  Tool was called {call_count} time(s) before guard tripped.\n")

    # ── Demo 2: force_final_answer=True ────────────────────────────────
    print("Demo 2: force_final_answer=True — guard injects a reminder, model responds\n")

    agent2 = Agent(
        model="demo-model",
        provider=LoopingProvider(),
        tools=empty_tools(SearchDocsTool()),
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        loop_guard=LoopGuard(max_identical_tool_calls=2, force_final_answer=True),
    )

    session2 = await agent2.session()
    async for event in session2.run("find a solution to my bug"):
        if isinstance(event, LoopGuardEvent):
            print(f"  ✓ LoopGuardEvent: action={event.action!r}, reason={event.reason!r}")
        elif isinstance(event, ResultEvent):
            symbol = "✓" if event.subtype == "success" else "✗"
            print(f"  {symbol} ResultEvent: subtype={event.subtype!r}", end="")
            if event.final_text:
                snippet = event.final_text[:60].replace("\n", " ")
                print(f', final_text="{snippet}..."')
            else:
                print()
        elif isinstance(event, ErrorEvent):
            print(f"  ✗ ErrorEvent: {event.error.get('name')}: {event.error.get('message')}")

    # ── Demo 3: guard disabled ─────────────────────────────────────────
    print("\nDemo 3: loop_guard=None — guard disabled, max_turns=3 limits the run\n")

    agent3 = Agent(
        model="demo-model",
        provider=LoopingProvider(),
        tools=empty_tools(SearchDocsTool()),
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        loop_guard=None,
        max_turns=3,
    )

    session3 = await agent3.session()
    async for event in session3.run("find a solution"):
        if isinstance(event, LoopGuardEvent):
            print(f"  ✓ LoopGuardEvent: reason={event.reason!r} (max_turns path)")
        elif isinstance(event, ResultEvent):
            print(f"  ResultEvent: subtype={event.subtype!r}")

    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
