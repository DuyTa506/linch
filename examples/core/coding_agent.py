"""Coding agent — a software-engineering assistant with file and shell access.

Run:
    DEEPSEEK_API_KEY=sk-... python3 examples/core/coding_agent.py
    # or any OpenAI-compatible key:
    OPENAI_API_KEY=sk-... python3 examples/core/coding_agent.py

Demonstrates:
  - tools_from_defaults() for the full SWE tool set (Read, Write, Edit,
    Bash, Glob, Grep)
  - BashRule to block destructive shell commands
  - PathRule to deny writes outside the project root
  - LoopGuard with force_final_answer so the agent always concludes
  - include_partial_messages=True to stream text as it arrives
  - Multi-turn: the session carries context across two tasks
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _make_agent():
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.loop_guard import LoopGuard
    from linch.permissions import BashRule, PathRule
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

    provider = OpenAIChatCompletionsProvider(
        OpenAIChatProviderOptions(api_key=api_key, base_url=base_url)
    )

    return Agent(
        model=model,
        provider=provider,
        tools=tools_from_defaults(),
        session_store=InMemorySessionStore(),
        permissions={
            "mode": "acceptEdits",           # auto-approve file reads/writes; ask for Bash
            "rules": [
                # Block destructive shell commands before the permission prompt.
                BashRule(pattern="rm -rf", decision="deny"),
                BashRule(pattern="sudo", decision="deny"),
                # Deny writes outside the project root.
                PathRule(paths=[str(ROOT / "**")], decision="allow"),
                PathRule(paths=["/**"], decision="deny"),
            ],
        },
        loop_guard=LoopGuard(
            max_identical_tool_calls=3,
            force_final_answer=True,     # always produce a text summary at the end
        ),
        system_prompt=(
            "You are a precise software-engineering assistant. "
            "Always read files before editing them. "
            "Run tests or linters after making changes if relevant. "
            "Keep your answers concise."
        ),
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        include_partial_messages=True,
        cwd=str(ROOT),
    )


async def main() -> None:
    agent = _make_agent()
    session = await agent.session()

    tasks = [
        "How many Python source files are in src/linch/providers/? List them.",
        "What is the return type of stream() in the BaseProvider class?",
    ]

    for task in tasks:
        print(f"\n{'─'*60}")
        print(f"Task: {task}")
        print("─" * 60)
        async for event in session.run(task):
            if event.type == "partial_assistant":
                if event.delta.get("kind") == "text":
                    print(event.delta["text"], end="", flush=True)
            elif event.type == "tool_call_start":
                print(f"\n[{event.tool_name}] {event.summary}")
            elif event.type == "tool_call_end" and event.is_error:
                print(f"  → ERROR: {event.result[:120]}")
            elif event.type == "result":
                if not event.final_text:
                    print("(no text response)")
                print(f"\n[done — {event.total_usage.output_tokens} output tokens]")
            elif event.type == "error":
                print(f"\nERROR: {event.error.get('message', '')}")

    await agent.close()


if __name__ == "__main__":
    asyncio.run(main())
