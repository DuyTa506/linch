"""Anthropic Claude provider with thinking blocks and prompt caching.

Run:
    ANTHROPIC_API_KEY=sk-ant-... python3 examples/providers/anthropic_agent.py

Requires `pip install 'linch[anthropic]'` and ANTHROPIC_API_KEY.

Demonstrates:
  1. AnthropicProvider — drop-in replacement for the OpenAI provider.
  2. Extended thinking — budget_tokens controls how long Claude can "think".
  3. Prompt caching — marks system blocks for cache reuse across turns.
  4. ThinkingBlock events in the event stream.
  5. Switching providers without changing any Agent config besides `provider`.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


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


async def run_basic() -> None:
    """Simple turn with AnthropicProvider — no thinking, no caching."""
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.providers.anthropic import AnthropicProvider, AnthropicProviderOptions
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools

    provider = AnthropicProvider(
        AnthropicProviderOptions(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    )

    agent = Agent(
        model="claude-haiku-4-5",
        provider=provider,
        tools=empty_tools(),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        loop_guard=None,
        system_prompt="Be concise.",
    )
    session = await agent.session()
    async for event in session.run("Reply with exactly: pong"):
        if event.type == "result":
            print(f"[basic] {event.final_text}")


async def run_with_thinking() -> None:
    """Turn with extended thinking enabled.

    ThinkingBlock events are streamed via PartialAssistantEvent while Claude
    reasons; the final text surfaces in ResultEvent.
    """
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.providers.anthropic import AnthropicProvider, AnthropicProviderOptions
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools

    provider = AnthropicProvider(
        AnthropicProviderOptions(
            api_key=os.environ.get("ANTHROPIC_API_KEY"),
            thinking={"type": "enabled", "budget_tokens": 2000},
        )
    )

    agent = Agent(
        model="claude-sonnet-4-6",
        provider=provider,
        tools=empty_tools(),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        loop_guard=None,
        include_partial_messages=True,
    )
    session = await agent.session()

    thinking_chars = 0
    async for event in session.run("What is 17 × 23? Show your reasoning."):
        if event.type == "partial_assistant":
            if event.delta.get("kind") == "thinking":
                thinking_chars += len(event.delta.get("text", ""))
        elif event.type == "result":
            print(f"[thinking] answer: {event.final_text}")
            print(f"[thinking] thinking chars streamed: {thinking_chars}")


async def run_with_caching() -> None:
    """Multi-turn conversation with prompt caching.

    Anthropic caches the system prompt and tool definitions on the first call;
    subsequent turns pay cache_read_tokens instead of input_tokens for those
    blocks.  Pass `cache_prompt=True` in RunOptions to enable per-run.
    """
    from linch import Agent, RunOptions
    from linch.config import FeatureFlags
    from linch.providers.anthropic import AnthropicProvider, AnthropicProviderOptions
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools

    provider = AnthropicProvider(
        AnthropicProviderOptions(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    )

    long_system = (
        "You are a helpful assistant for a software team. "
        "This system prompt is intentionally long so it is worth caching. "
        + ("Always be concise and accurate. " * 20)
    )

    agent = Agent(
        model="claude-haiku-4-5",
        provider=provider,
        tools=empty_tools(),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        loop_guard=None,
        system_prompt=long_system,
    )
    session = await agent.session()

    for turn, prompt in enumerate(["What is 2 + 2?", "What is 3 + 3?"], 1):
        cache_tokens = 0
        input_tokens = 0
        async for event in session.run(prompt, RunOptions(cache_prompt=True)):
            if event.type == "usage":
                cache_tokens += getattr(event.usage, "cache_read_tokens", 0)
                input_tokens += event.usage.input_tokens
            elif event.type == "result":
                print(f"[cache] turn {turn}: {event.final_text}")
        print(f"         input={input_tokens}  cache_read={cache_tokens}")


async def main() -> None:
    load_project_env()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set — set it to run this example.")
        print("Install Anthropic extra: pip install 'linch[anthropic]'")
        return

    print("=== Basic ===")
    await run_basic()

    print("\n=== Thinking ===")
    await run_with_thinking()

    print("\n=== Caching ===")
    await run_with_caching()


if __name__ == "__main__":
    asyncio.run(main())
