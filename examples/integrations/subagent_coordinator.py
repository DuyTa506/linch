"""Subagent coordinator — parent agent delegates to typed subagents.

Run:
    python3 examples/integrations/subagent_coordinator.py

Requires OPENAI_API_KEY for the live demo.  The local section shows how to
load and inspect agent definitions from disk without making a provider call.

Demonstrates:
  1. Agent definition files (Markdown with YAML frontmatter) loaded from
     `.linch/agents/` inside a config directory.
  2. tools filter — a subagent only receives a subset of the parent's tools.
  3. SubagentEvent bubbling — child events appear in the parent event stream.
  4. Agent(config_dir=...) — point to a custom config directory.
  5. Built-in `verification` subagent — available without a disk definition.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# ── Agent definition files ───────────────────────────────────────────────────
# Each file lives at {config_dir}/agents/<name>.md and declares a role via
# YAML frontmatter, plus a Markdown body that becomes the subagent's system prompt.

RESEARCHER_MD = """\
---
name: researcher
description: Searches and summarises information on a given topic.
tools:
  - Glob
  - Grep
  - Read
---
You are a focused research subagent.
Given a topic, use your tools to gather relevant information and return
a concise, structured summary.  Do not perform any writes.
"""

SUMMARISER_MD = """\
---
name: summariser
description: Takes raw text and returns a polished one-paragraph summary.
tools: []
---
You are a summarisation subagent.
Read the text in the user message and return a single, polished paragraph
summarising the key points. Do not use any tools.
"""

VERIFY_AFTER_CHANGES_PROMPT = """\
Use Subagent with subagent_type="verification" after these changes.
Original task: Fix the session cleanup bug.
Artifacts changed: src/linch/session.py, tests/storage/test_sessions.py.
Approach taken: close child sessions before removing them from the in-memory registry.
Verify with the relevant session tests plus one adversarial regression check.
"""


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


async def demo_loader() -> None:
    """Show loading agent definitions from disk — no API call needed."""
    from linch.subagents.loader import load_agents_from_dir
    from linch.subagents.registry import AgentRegistry

    with tempfile.TemporaryDirectory() as tmp:
        agents_dir = Path(tmp) / "agents"
        agents_dir.mkdir()
        (agents_dir / "researcher.md").write_text(RESEARCHER_MD)
        (agents_dir / "summariser.md").write_text(SUMMARISER_MD)

        result = await load_agents_from_dir(tmp)
        registry = AgentRegistry(result.agents)

        print(f"Loaded {len(result.agents)} agent definition(s):")
        for agent in registry.list():
            tools_note = (
                f"  tools={agent.frontmatter.tools}" if agent.frontmatter.tools else "  tools=all"
            )
            print(f"  [{agent.name}] {agent.frontmatter.description}{tools_note}")

        researcher = registry.get("researcher")
        assert researcher is not None
        print(f"\nResearcher body preview:\n  {researcher.body.splitlines()[0]!r}")
        verification = registry.get("verification")
        assert verification is not None
        print(
            "\nBuilt-in verifier:"
            f"\n  [{verification.name}] {verification.frontmatter.description}"
        )
        print(f"\nExample parent prompt for verification:\n{VERIFY_AFTER_CHANGES_PROMPT}")


async def demo_live_subagents() -> None:
    """Live: parent agent asks the LLM to spawn a researcher subagent."""
    load_project_env()
    if not os.environ.get("OPENAI_API_KEY"):
        print("\nOPENAI_API_KEY not set; skipping live subagent demo.")
        return

    from linch import Agent
    from linch.config import FeatureFlags
    from linch.events import SubagentEvent
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import tools_from_defaults

    with tempfile.TemporaryDirectory() as tmp:
        agents_dir = Path(tmp) / "agents"
        agents_dir.mkdir()
        (agents_dir / "researcher.md").write_text(RESEARCHER_MD)
        (agents_dir / "summariser.md").write_text(SUMMARISER_MD)

        agent = Agent(
            model="gpt-5-nano-2025-08-07",
            openai_api_key=os.environ.get("OPENAI_API_KEY"),
            tools=tools_from_defaults(),
            session_store=InMemorySessionStore(),
            permissions={"mode": "skip-dangerous"},
            config_dir=tmp,
            features=FeatureFlags(skills=False, mcp=False, subagents=True),
            system_prompt=(
                "You are a coordinator. When the user asks you to research something, "
                "spawn a 'researcher' subagent with the task. Then spawn a 'summariser' "
                "subagent to polish the output."
            ),
        )

        session = await agent.session()
        subagent_events = 0

        async for event in session.run(
            "Research what files are in the current directory and give me a tidy summary."
        ):
            if isinstance(event, SubagentEvent):
                subagent_events += 1
                inner = event.event
                if inner.type == "result":
                    print(f"  [subagent:{event.display_name}] result → {inner.final_text[:80]}")
            elif event.type == "result":
                print(f"\nParent final answer:\n{event.final_text}")

        print(f"\nSubagent events received: {subagent_events}")


async def main() -> None:
    print("── Local: agent definition loader ──────────────────────────────")
    await demo_loader()

    print("\n── Live: parent + subagent delegation ──────────────────────────")
    await demo_live_subagents()


if __name__ == "__main__":
    asyncio.run(main())
