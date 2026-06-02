"""Multi-agent context isolation.

Why this matters
----------------
Every message, tool call, and tool result the coordinator processes accumulates in
its ``provider_view``.  For complex tasks — crawling 50 files, running 10 searches,
summarising 200 pages — that alone can consume the entire context window.

Subagents break this: each child session starts with ``provider_view = []`` and runs
its own agent loop in full isolation.  Only the final text answer (a few hundred tokens)
bubbles back to the coordinator.  The coordinator's context stays bounded regardless of
how much work the children do.

                coordinator (provider_view stays small)
                 /              |              \\
          researcher         analyzer        reporter
          (0 → 40k           (0 → 60k        (0 → 10k
           tokens,            tokens,         tokens,
           discarded)         discarded)      discarded)
               \\               |              /
                ↓               ↓             ↓
              200-token summary per child reaches coordinator

Patterns in this file
---------------------
1. Local demo: isolation mechanics — no API key needed.
   Shows provider_view sizes and that child work never enters parent context.
2. Live demo: sequential pipeline (discover → analyse → report).
3. Live demo: parallel analysts — independent children run concurrently.

Run:
    python3 examples/integrations/multi_agent_isolation.py
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# ── Agent definition files ────────────────────────────────────────────────────
# Each definition is a Markdown file with YAML frontmatter.  The body becomes
# the subagent's system prompt appended after the standard AgentKit blocks.

RESEARCHER_MD = """\
---
name: researcher
description: Searches and collects raw information for a given topic.
tools:
  - Glob
  - Grep
  - Read
---
You are a focused research subagent.  You have NO history from the coordinator —
start fresh.  Use Glob/Grep/Read to gather everything relevant to the task, then
return a structured summary.  Include file names and key findings.  Be thorough:
the coordinator will NOT call you again for follow-up details.
"""

ANALYSER_MD = """\
---
name: analyser
description: Takes raw findings and identifies patterns, risks, or action items.
tools:
  - Read
---
You are an analysis subagent.  The user message contains raw research findings.
Read any referenced files if you need detail, then return a bulleted analysis
(what is good, what is risky, what should change).  Be specific — cite file names
and line ranges where relevant.
"""

REPORTER_MD = """\
---
name: reporter
description: Synthesises multiple analyses into a single executive report.
tools: []
---
You are a reporting subagent.  You have no tools.  The user message contains
structured analyses from specialist agents.  Synthesise them into a concise,
well-structured executive report.  Use headers.  No tool calls needed.
"""


def _load_env() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))


# ── 1. Local demo: isolation mechanics ───────────────────────────────────────


class ScriptedProvider:
    """Fake provider that returns canned responses in order.

    Each call gets the next response from the script.  Use this to simulate
    a full multi-turn subagent run without a live API key.
    """

    def __init__(self, responses: list[str]) -> None:
        self._responses = responses
        self._idx = 0

    def context_window(self, model: str) -> int:
        return 128_000

    async def stream(self, req):
        from agent_kit.types import Usage

        text = self._responses[self._idx % len(self._responses)]
        self._idx += 1
        yield {"type": "message_start", "model": req.model}
        yield {"type": "text_delta", "text": text}
        yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}


async def demo_isolation_mechanics() -> None:
    """Show with concrete numbers that child work never enters parent context."""
    from agent_kit import Agent
    from agent_kit.config import FeatureFlags
    from agent_kit.sessions import InMemorySessionStore
    from agent_kit.subagents.runner import RunSubagentArgs, RunSubagentResult, run_subagent
    from agent_kit.subagents.types import AgentDefinition, AgentFrontmatter

    # A provider that returns a realistic multi-paragraph research summary
    LONG_SUMMARY = (
        "Research complete.  Found 12 relevant Python source files.\n\n"
        "Key findings:\n"
        "- src/agent_kit/scheduler.py (312 lines): parallel tool execution with\n"
        "  ResourceAccess conflict detection.\n"
        "- src/agent_kit/loop.py (418 lines): main agent loop; calls provider.stream()\n"
        "  and emits events.\n"
        "- src/agent_kit/session.py: provider_view vs full_history separation.\n\n"
        "No blocking I/O found in the hot path.  All disk operations use\n"
        "asyncio.to_thread().  Recommend adding timeout guards to provider calls."
    )

    provider = ScriptedProvider([LONG_SUMMARY])

    agent = Agent(
        model="gpt-5",
        provider=provider,
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        result_offload=None,  # keep clean for this demo
    )

    # Create parent session — empty to start
    parent = await agent.session()
    assert len(parent.provider_view) == 0, "parent starts empty"

    # Define a researcher inline (no disk files needed)
    definition = AgentDefinition(
        name="researcher",
        file_path="<inline>",
        source="built-in",
        frontmatter=AgentFrontmatter(
            name="researcher",
            description="Searches codebase and returns findings.",
            tools=["Glob", "Grep", "Read"],
        ),
        body=RESEARCHER_MD.split("---", 2)[2].strip(),
    )

    print("── Isolation mechanics ──────────────────────────────────────────")
    print(f"  Parent provider_view BEFORE subagent: {len(parent.provider_view)} messages")

    result: RunSubagentResult = await run_subagent(
        RunSubagentArgs(
            parent_session=parent,
            parent_agent=agent,
            definition=definition,
            prompt="Analyse the AgentKit scheduler for concurrency issues.",
            display_name="researcher",
            subagent_run_id="demo_001",
        )
    )

    print(f"  Parent provider_view AFTER  subagent: {len(parent.provider_view)} messages")
    print(f"  Child errored:  {result.errored}")
    print(f"  Text returned to coordinator ({len(result.final_text)} chars):")
    for line in result.final_text.splitlines()[:6]:
        print(f"    {line}")
    print()
    print("  ↳ The child ran a full agent loop but added ZERO messages to the")
    print("    parent context.  The coordinator only received the final summary.")
    print()


# ── 2. Live: sequential pipeline ─────────────────────────────────────────────


async def demo_sequential_pipeline() -> None:
    """Coordinator → researcher → analyser → reporter.

    The coordinator orchestrates via the Subagent tool.  Each child handles its
    own task in isolation; the coordinator never sees the children's raw tool
    output — only their final text.
    """
    _load_env()
    if not os.environ.get("OPENAI_API_KEY"):
        print("── Sequential pipeline (skipped — OPENAI_API_KEY not set) ──────")
        return

    from agent_kit import Agent
    from agent_kit.config import FeatureFlags
    from agent_kit.events import SubagentEvent
    from agent_kit.sessions import InMemorySessionStore
    from agent_kit.tools.registry import tools_from_defaults

    with tempfile.TemporaryDirectory() as tmp:
        agents_dir = Path(tmp) / "agents"
        agents_dir.mkdir()
        (agents_dir / "researcher.md").write_text(RESEARCHER_MD)
        (agents_dir / "analyser.md").write_text(ANALYSER_MD)
        (agents_dir / "reporter.md").write_text(REPORTER_MD)

        agent = Agent(
            model="gpt-4o-mini",
            openai_api_key=os.environ.get("OPENAI_API_KEY"),
            tools=tools_from_defaults(),
            session_store=InMemorySessionStore(),
            permissions={"mode": "skip-dangerous"},
            config_dir=tmp,
            features=FeatureFlags(skills=False, mcp=False, subagents=True),
            system_prompt=(
                "You are a coordinator.  When given a codebase analysis task:\n"
                "1. Spawn a 'researcher' subagent with the exploration task.\n"
                "2. Pass the researcher's output to an 'analyser' subagent.\n"
                "3. Pass the analysis to a 'reporter' subagent for the final report.\n"
                "4. Return the reporter's output verbatim as your final answer.\n\n"
                "Never do the research yourself — delegate everything to the subagents.\n"
                "Each subagent starts with NO history — provide full context in the prompt."
            ),
        )

        session = await agent.session()
        child_turns: dict[str, int] = {}

        print("── Sequential pipeline ──────────────────────────────────────────")
        async for event in session.run(
            f"Analyse the Python files in {ROOT / 'src' / 'agent_kit' / 'tools'} "
            "for quality issues and produce an executive report."
        ):
            if isinstance(event, SubagentEvent):
                inner = event.event
                child_turns[event.display_name] = child_turns.get(event.display_name, 0) + 1
                if inner.type == "result":
                    print(
                        f"  [{event.display_name}] finished "
                        f"({child_turns[event.display_name]} events, "
                        f"result: {len(inner.final_text)} chars)"
                    )
            elif event.type == "result":
                print(f"\nCoordinator report ({len(event.final_text)} chars):")
                for line in event.final_text.splitlines()[:15]:
                    print(f"  {line}")
                if len(event.final_text.splitlines()) > 15:
                    print("  ...")

        parent_msgs = len(session.provider_view)
        print(f"\nCoordinator provider_view: {parent_msgs} messages")
        print("↳ That's the coordinator's entire context — not the children's work.")
    print()


# ── 3. Live: parallel analysts ────────────────────────────────────────────────


async def demo_parallel_analysts() -> None:
    """Spawn multiple analyser subagents in parallel, then synthesise.

    The coordinator tells the LLM to call Subagent multiple times in one turn.
    Because the scheduler treats independent tool calls in parallel, the children
    run concurrently and the coordinator only waits for the slowest one.
    """
    _load_env()
    if not os.environ.get("OPENAI_API_KEY"):
        print("── Parallel analysts (skipped — OPENAI_API_KEY not set) ─────────")
        return

    from agent_kit import Agent
    from agent_kit.config import FeatureFlags
    from agent_kit.events import SubagentEvent
    from agent_kit.sessions import InMemorySessionStore
    from agent_kit.tools.registry import tools_from_defaults

    SUBSYSTEMS = [
        ("tools/", "tool implementations"),
        ("providers/", "provider adapters"),
        ("memory/", "memory subsystem"),
    ]
    task_list = "\n".join(
        f"- Subsystem '{name}' at {ROOT / 'src' / 'agent_kit' / path}: {focus}"
        for path, focus in SUBSYSTEMS
    )

    with tempfile.TemporaryDirectory() as tmp:
        agents_dir = Path(tmp) / "agents"
        agents_dir.mkdir()
        (agents_dir / "analyser.md").write_text(ANALYSER_MD)
        (agents_dir / "reporter.md").write_text(REPORTER_MD)

        agent = Agent(
            model="gpt-4o-mini",
            openai_api_key=os.environ.get("OPENAI_API_KEY"),
            tools=tools_from_defaults(),
            session_store=InMemorySessionStore(),
            permissions={"mode": "skip-dangerous"},
            config_dir=tmp,
            features=FeatureFlags(skills=False, mcp=False, subagents=True),
            system_prompt=(
                "You are a parallel coordinator.\n"
                "Given a list of subsystems:\n"
                "1. Spawn ONE 'analyser' subagent per subsystem IN THE SAME TURN "
                "(parallel Subagent calls).  Give each its subsystem path and focus.\n"
                "2. After all analysts finish, spawn ONE 'reporter' subagent with all "
                "analyses combined.\n"
                "3. Return the reporter's output as your answer.\n\n"
                "Key: issue all analyst Subagent calls in a single response turn so they "
                "run concurrently.  Do not wait for one before spawning the next."
            ),
        )

        session = await agent.session()
        active_children: set[str] = set()

        print("── Parallel analysts ────────────────────────────────────────────")
        async for event in session.run(
            f"Analyse these AgentKit subsystems in parallel:\n{task_list}\n\n"
            "Then synthesise the findings into one executive report."
        ):
            if isinstance(event, SubagentEvent):
                inner = event.event
                if inner.type in ("tool_call_start", "assistant"):
                    active_children.add(event.display_name)
                if inner.type == "result":
                    active_children.discard(event.display_name)
                    print(
                        f"  [{event.display_name}] done — "
                        f"{len(inner.final_text)} chars returned to coordinator"
                    )
            elif event.type == "result":
                print(f"\nFinal report ({len(event.final_text)} chars):")
                for line in event.final_text.splitlines()[:12]:
                    print(f"  {line}")
                if len(event.final_text.splitlines()) > 12:
                    print("  ...")
    print()


# ── 4. Combined: subagent + filesystem offload ────────────────────────────────


async def demo_subagent_with_offload() -> None:
    """Each subagent has its own session filesystem.

    Large search results inside a subagent are offloaded to that child's
    StateFileBackend.  The offloaded blobs are discarded when the child
    finishes — they never reach the coordinator's context.  Only the
    child's final text answer crosses the boundary.

    This is the recommended pattern for RAG pipelines:
      coordinator → [rag_agent (reads 200k docs, offloads results)]
                         ↓ returns a 3-bullet summary
      coordinator context: 3 bullets, not 200k
    """
    _load_env()
    if not os.environ.get("OPENAI_API_KEY"):
        print("── Subagent + filesystem offload (skipped — OPENAI_API_KEY not set) ──")
        return

    from agent_kit import Agent
    from agent_kit.config import FeatureFlags
    from agent_kit.events import SubagentEvent
    from agent_kit.filesystem import DiskFileBackend, OffloadConfig
    from agent_kit.sessions import InMemorySessionStore
    from agent_kit.tools.registry import tools_from_defaults

    with tempfile.TemporaryDirectory() as tmp:
        agents_dir = Path(tmp) / "agents"
        agents_dir.mkdir()
        (agents_dir / "researcher.md").write_text(RESEARCHER_MD)

        agent = Agent(
            model="gpt-4o-mini",
            openai_api_key=os.environ.get("OPENAI_API_KEY"),
            tools=tools_from_defaults(),
            session_store=InMemorySessionStore(),
            permissions={"mode": "skip-dangerous"},
            config_dir=tmp,
            features=FeatureFlags(skills=False, mcp=False, subagents=True),
            # Filesystem offload is on by default — each session (including child
            # sessions) gets its own StateFileBackend.  Large tool results are
            # offloaded inside the child and discarded when the child finishes.
            filesystem=DiskFileBackend(Path(tmp) / "offload"),
            result_offload=OffloadConfig(threshold_tokens=2_000, preview_lines=5),
            system_prompt=(
                "You are a coordinator.  Delegate all research to the 'researcher' subagent. "
                "Large file reads inside the subagent will be offloaded automatically — "
                "the subagent will use read_file() to retrieve what it needs, then return "
                "a compact summary.  Return that summary as your answer."
            ),
        )

        session = await agent.session()

        print("── Subagent + filesystem offload ────────────────────────────────")
        async for event in session.run(
            f"Use the researcher subagent to summarise the public API surface of "
            f"the AgentKit tools module at {ROOT / 'src' / 'agent_kit' / 'tools'}."
        ):
            if isinstance(event, SubagentEvent):
                inner = event.event
                if inner.type == "tool_call_end" and getattr(inner, "tool_name", "") == "Read":
                    offloaded = inner.result != inner.tool_result.content if inner.tool_result else False
                    tag = " [offloaded]" if offloaded else ""
                    print(f"    [researcher] Read → {len(inner.result)} chars{tag}")
                if inner.type == "result":
                    print(f"  [researcher] summary ({len(inner.final_text)} chars):")
                    for line in inner.final_text.splitlines()[:5]:
                        print(f"    {line}")
            elif event.type == "result":
                print(f"\nCoordinator answer ({len(event.final_text)} chars).")
    print()


async def main() -> None:
    print("AgentKit — multi-agent context isolation\n")

    await demo_isolation_mechanics()
    await demo_sequential_pipeline()
    await demo_parallel_analysts()
    await demo_subagent_with_offload()


if __name__ == "__main__":
    asyncio.run(main())
