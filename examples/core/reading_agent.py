"""Reading agent — a read-only codebase Q&A assistant.

Run:
    DEEPSEEK_API_KEY=sk-... python3 examples/core/reading_agent.py
    OPENAI_API_KEY=sk-... python3 examples/core/reading_agent.py

Demonstrates:
  - Read-only tool set: tools_from_defaults(exclude={"Write", "Edit", "Bash"})
    → the model can Read, Glob, Grep but cannot modify anything
  - PathRule to fence access to the project root only
  - mode="default" so any unexpected tool call still requires approval
  - Multi-turn: context carries across questions about the same codebase
  - Custom system prompt replacing the SWE identity with a reviewer persona

Use this pattern when you want an agent that can *understand* a codebase
but must never touch it — code review, documentation generation, onboarding
Q&A, security audits, etc.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _make_agent():
    from linch import Agent
    from linch.config import FeatureFlags, SystemPromptConfig
    from linch.loop_guard import LoopGuard
    from linch.permissions import PathRule
    from linch.providers import OpenAIChatCompletionsProvider
    from linch.providers.openai_chat import OpenAIChatProviderOptions
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import tools_from_defaults

    api_key = (
        os.environ.get("DEEPSEEK_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("OPENAPI_KEY")
    )
    if not api_key:
        raise SystemExit("Set DEEPSEEK_API_KEY or OPENAI_API_KEY and re-run.")

    base_url = "https://api.deepseek.com" if os.environ.get("DEEPSEEK_API_KEY") else None
    model = "deepseek-v4-flash" if base_url else "gpt-5-nano-2025-08-07"

    # Read, Glob, Grep — no Write, Edit, or Bash.
    tools = tools_from_defaults(exclude={"Write", "Edit", "Bash"})

    return Agent(
        model=model,
        provider=OpenAIChatCompletionsProvider(
            OpenAIChatProviderOptions(api_key=api_key, base_url=base_url)
        ),
        tools=tools,
        session_store=InMemorySessionStore(),
        permissions={
            "mode": "skip-dangerous",
            "rules": [
                # Only allow reads inside the project; deny everything outside.
                PathRule(paths=[str(ROOT / "**")], decision="allow"),
                PathRule(paths=["/**"], decision="deny"),
            ],
        },
        loop_guard=LoopGuard(max_identical_tool_calls=2, force_final_answer=True),
        max_turns=10,
        system_prompt_config=SystemPromptConfig(
            replace_defaults=True,
            append=(
                "You are a senior engineer doing a deep-read of this codebase. "
                "You have read-only access: you may search, grep, and read files "
                "but you cannot modify anything. "
                "Give precise, evidence-based answers — quote file paths and line "
                "numbers when relevant. Be concise."
            ),
        ),
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        include_partial_messages=True,
        cwd=str(ROOT),
    )


async def main() -> None:
    agent = _make_agent()
    session = await agent.session()

    questions = [
        "What file is src/linch/loop.py and what does run_loop() do in one sentence?",
        "What are the names of the three provider classes in src/linch/providers/?",
    ]

    for question in questions:
        print(f"\n{'─'*60}")
        print(f"Q: {question}")
        print("─" * 60)
        async for event in session.run(question):
            if event.type == "partial_assistant":
                if event.delta.get("kind") == "text":
                    print(event.delta["text"], end="", flush=True)
            elif event.type == "tool_call_start":
                print(f"\n  [{event.tool_name}] {event.summary}")
            elif event.type == "result":
                if not event.final_text:
                    print("(no answer)")
                print()
            elif event.type == "error":
                print(f"\nERROR: {event.error.get('message', '')}")

    await agent.close()


if __name__ == "__main__":
    asyncio.run(main())
