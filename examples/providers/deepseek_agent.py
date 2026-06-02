"""DeepSeek provider examples — OpenAI-compatible and Anthropic-compatible endpoints.

Run:
    DEEPSEEK_API_KEY=sk-... python3 examples/providers/deepseek_agent.py

DeepSeek exposes two endpoints:
  - OpenAI-compatible:    https://api.deepseek.com            → OpenAIChatCompletionsProvider
  - Anthropic-compatible: https://api.deepseek.com/anthropic  → AnthropicProvider

Model notes (as of 2026-06):
  - deepseek-v4-flash / deepseek-v4-pro: reasoning models that return
    `reasoning_content` on every turn.  Linch's OpenAI Chat provider does not
    yet round-trip reasoning_content, so multi-turn tool-use loops will get a 400
    error.  Use these models with empty_tools() (no multi-turn tool calls), OR use
    the Anthropic-compatible endpoint where reasoning is handled as thinking blocks.
  - deepseek-chat (deprecated 2026-07-24): classic non-reasoning chat model;
    works fine with tool calls over the OpenAI-compatible endpoint.

Demonstrates:
  1. OpenAI-compatible path: Q&A without tools, multi-turn context.
  2. Anthropic-compatible path: Q&A, works with reasoning models too.
  3. Tool use via deepseek-chat (non-reasoning, OpenAI path).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEEPSEEK_BASE_OPENAI = "https://api.deepseek.com"
DEEPSEEK_BASE_ANTHROPIC = "https://api.deepseek.com/anthropic"

MODEL_FLASH = "deepseek-v4-flash"  # reasoning — no tool loops
MODEL_CHAT = "deepseek-chat"  # classic — tool loops OK


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


# ── 1. OpenAI-compatible: basic Q&A (no tools — avoids reasoning_content issue) ──


async def run_openai_basic(api_key: str) -> None:
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.providers import OpenAIChatCompletionsProvider
    from linch.providers.openai_chat import OpenAIChatProviderOptions
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools

    provider = OpenAIChatCompletionsProvider(
        OpenAIChatProviderOptions(api_key=api_key, base_url=DEEPSEEK_BASE_OPENAI)
    )
    agent = Agent(
        model=MODEL_FLASH,
        provider=provider,
        tools=empty_tools(),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        loop_guard=None,
        system_prompt="Be concise.",
    )
    session = await agent.session()
    async for event in session.run("What is 7 × 8? Answer with just the number."):
        if event.type == "result":
            print(f"[openai/{MODEL_FLASH}] {event.final_text}")
        elif event.type == "error":
            print(f"[openai/{MODEL_FLASH}] ERROR: {event.error.get('message', '')}")


# ── 2. OpenAI-compatible: tool use with deepseek-chat (non-reasoning model) ───
#
# deepseek-chat tends to repeat tool calls without self-stopping, so set
# force_final_answer=True on the LoopGuard to extract a text reply anyway.


async def run_openai_tools(api_key: str) -> None:
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.loop_guard import LoopGuard
    from linch.providers import OpenAIChatCompletionsProvider
    from linch.providers.openai_chat import OpenAIChatProviderOptions
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import tools_from_defaults

    provider = OpenAIChatCompletionsProvider(
        OpenAIChatProviderOptions(api_key=api_key, base_url=DEEPSEEK_BASE_OPENAI)
    )
    agent = Agent(
        model=MODEL_CHAT,
        provider=provider,
        tools=tools_from_defaults(),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        loop_guard=LoopGuard(max_identical_tool_calls=3, force_final_answer=True),
        system_prompt="Be concise. Use tools when relevant.",
    )
    session = await agent.session()
    async for event in session.run("Run: echo hello"):
        if event.type == "result":
            print(f"[openai/{MODEL_CHAT}/tools] {event.final_text or '(no text)'}")
        elif event.type == "error":
            print(f"[openai/{MODEL_CHAT}/tools] ERROR: {event.error.get('message', '')}")


# ── 3. Anthropic-compatible: works with reasoning models too ──────────────────


async def run_anthropic_path(api_key: str) -> None:
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.providers.anthropic import AnthropicProvider, AnthropicProviderOptions
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools

    provider = AnthropicProvider(
        AnthropicProviderOptions(api_key=api_key, base_url=DEEPSEEK_BASE_ANTHROPIC)
    )
    agent = Agent(
        model=MODEL_FLASH,
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
            print(f"[anthropic/{MODEL_FLASH}] {event.final_text}")
        elif event.type == "error":
            print(f"[anthropic/{MODEL_FLASH}] ERROR: {event.error.get('message', '')}")


# ── 4. Multi-turn: context preserved across turns ─────────────────────────────


async def run_multi_turn(api_key: str) -> None:
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.providers import OpenAIChatCompletionsProvider
    from linch.providers.openai_chat import OpenAIChatProviderOptions
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools

    provider = OpenAIChatCompletionsProvider(
        OpenAIChatProviderOptions(api_key=api_key, base_url=DEEPSEEK_BASE_OPENAI)
    )
    agent = Agent(
        model=MODEL_FLASH,
        provider=provider,
        tools=empty_tools(),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        loop_guard=None,
    )
    session = await agent.session()

    for prompt in ["My favourite number is 42.", "What is my favourite number?"]:
        async for event in session.run(prompt):
            if event.type == "result":
                print(f"[multi-turn] Q: {prompt!r:50s}  A: {event.final_text}")


# ── Entry point ────────────────────────────────────────────────────────────────


async def main() -> None:
    load_project_env()
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        print("DEEPSEEK_API_KEY not set — export it and re-run.")
        return

    print("=== 1. OpenAI path (deepseek-v4-flash, no tools) ===")
    await run_openai_basic(api_key)

    print("\n=== 2. OpenAI path (deepseek-chat, with tools) ===")
    await run_openai_tools(api_key)

    print("\n=== 3. Anthropic path (deepseek-v4-flash) ===")
    await run_anthropic_path(api_key)

    print("\n=== 4. Multi-turn context (deepseek-v4-flash, no tools) ===")
    await run_multi_turn(api_key)


if __name__ == "__main__":
    asyncio.run(main())
