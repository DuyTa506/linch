"""DeepSeek provider examples — OpenAI-compatible and Anthropic-compatible endpoints.

Run:
    DEEPSEEK_API_KEY=sk-... python3 examples/providers/deepseek_agent.py

DeepSeek exposes two endpoints:
  - OpenAI-compatible:    https://api.deepseek.com            → OpenAIChatCompletionsProvider
  - Anthropic-compatible: https://api.deepseek.com/anthropic  → AnthropicProvider

Model notes (as of 2026-06):
  - deepseek-v4-flash / deepseek-v4-pro: reasoning models.  Linch round-trips
    `reasoning_content` automatically, so tool-use loops work correctly over the
    OpenAI-compatible endpoint.
  - deepseek-chat (deprecated 2026-07-24): classic non-reasoning chat model.

Demonstrates:
  1. OpenAI path: Q&A without tools (no thinking events).
  2. OpenAI path: thinking events visible (reasoning_content streamed).
  3. OpenAI path: tool use (reasoning model, multi-turn).
  4. OpenAI path: thinking + tool use (thinking visible during tool loops).
  5. Anthropic path: Q&A (basic).
  6. Anthropic path: thinking enabled.
  7. Multi-turn context preserved across turns (OpenAI path).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEEPSEEK_BASE_OPENAI = "https://api.deepseek.com"
DEEPSEEK_BASE_ANTHROPIC = "https://api.deepseek.com/anthropic"

MODEL_FLASH = "deepseek-v4-flash"  # reasoning — tool loops work (reasoning_content round-tripped)
MODEL_CHAT = "deepseek-chat"  # classic non-reasoning (deprecated 2026-07-24)


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


def _make_openai_provider(api_key: str) -> "OpenAIChatCompletionsProvider":
    from linch.providers import OpenAIChatCompletionsProvider
    from linch.providers.openai_chat import OpenAIChatProviderOptions

    return OpenAIChatCompletionsProvider(
        OpenAIChatProviderOptions(api_key=api_key, base_url=DEEPSEEK_BASE_OPENAI)
    )


def _make_anthropic_provider(api_key: str) -> "AnthropicProvider":
    from linch.providers.anthropic import AnthropicProvider, AnthropicProviderOptions

    return AnthropicProvider(
        AnthropicProviderOptions(api_key=api_key, base_url=DEEPSEEK_BASE_ANTHROPIC)
    )


# ── 1. OpenAI path: basic Q&A (thinking not surfaced) ────────────────────────


async def run_openai_basic(api_key: str) -> None:
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools

    agent = Agent(
        model=MODEL_FLASH,
        provider=_make_openai_provider(api_key),
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
            print(f"[openai/basic] {event.final_text}")
        elif event.type == "error":
            print(f"[openai/basic] ERROR: {event.error.get('message', '')}")


# ── 2. OpenAI path: thinking events visible ───────────────────────────────────


async def run_openai_thinking(api_key: str) -> None:
    """Stream reasoning_content as thinking events.

    Set include_partial_messages=True so PartialAssistantEvent is emitted for
    each reasoning_content chunk.  The delta has kind="thinking" for thinking
    chunks and kind="text" for answer chunks.
    """
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools

    agent = Agent(
        model=MODEL_FLASH,
        provider=_make_openai_provider(api_key),
        tools=empty_tools(),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        loop_guard=None,
        system_prompt="Be concise.",
        include_partial_messages=True,
    )
    session = await agent.session()

    thinking_chars = 0
    async for event in session.run("What is 17 × 23? Show your reasoning."):
        if event.type == "partial_assistant":
            if event.delta.get("kind") == "thinking":
                thinking_chars += len(event.delta.get("text", ""))
        elif event.type == "result":
            print(f"[openai/thinking] answer: {event.final_text}")
            print(f"[openai/thinking] thinking chars streamed: {thinking_chars}")
        elif event.type == "error":
            print(f"[openai/thinking] ERROR: {event.error.get('message', '')}")


# ── 3. OpenAI path: tool use (reasoning model, multi-turn) ───────────────────
#
# reasoning_content is captured during streaming and round-tripped in the
# assistant message on subsequent calls, so multi-turn tool loops work.


async def run_openai_tools(api_key: str) -> None:
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.loop_guard import LoopGuard
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import tools_from_defaults

    agent = Agent(
        model=MODEL_FLASH,
        provider=_make_openai_provider(api_key),
        tools=tools_from_defaults(),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        loop_guard=LoopGuard(max_identical_tool_calls=3, force_final_answer=True),
        system_prompt="Be concise. Use the Bash tool to run shell commands.",
    )
    session = await agent.session()

    # Turn 1 — tool call; reasoning_content is captured in the session history.
    async for event in session.run("Run this shell command and show the output: echo hello-linch"):
        if event.type == "tool_call_end":
            print(f"[openai/tools] tool: {event.tool_name} → {str(event.result)[:60]!r}")
        elif event.type == "result":
            print(f"[openai/tools] turn 1: {event.final_text or '(no text)'}")
        elif event.type == "error":
            print(f"[openai/tools] ERROR: {event.error.get('message', '')}")

    # Turn 2 — reasoning_content from turn 1 is round-tripped; no 400 error.
    async for event in session.run("Now run: echo second-turn"):
        if event.type == "tool_call_end":
            print(f"[openai/tools] tool: {event.tool_name} → {str(event.result)[:60]!r}")
        elif event.type == "result":
            print(f"[openai/tools] turn 2: {event.final_text or '(no text)'}")
        elif event.type == "error":
            print(f"[openai/tools] ERROR: {event.error.get('message', '')}")


# ── 4. OpenAI path: thinking visible during tool loops ───────────────────────


async def run_openai_thinking_with_tools(api_key: str) -> None:
    """Thinking events and tool use in the same loop.

    The model emits reasoning_content before each tool call; stream_turn
    flushes it into a ThinkingBlock that is round-tripped on the next turn.
    """
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.loop_guard import LoopGuard
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import tools_from_defaults

    agent = Agent(
        model=MODEL_FLASH,
        provider=_make_openai_provider(api_key),
        tools=tools_from_defaults(),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        loop_guard=LoopGuard(max_identical_tool_calls=3, force_final_answer=True),
        system_prompt="Be concise. Use the Bash tool to run shell commands.",
        include_partial_messages=True,
    )
    session = await agent.session()

    thinking_chars = 0
    async for event in session.run("Run: python3 -c \"print(6*7)\" and tell me the result."):
        if event.type == "partial_assistant":
            if event.delta.get("kind") == "thinking":
                thinking_chars += len(event.delta.get("text", ""))
        elif event.type == "tool_call_end":
            print(
                f"[openai/thinking+tools] tool: {event.tool_name} → {str(event.result)[:60]!r}"
            )
        elif event.type == "result":
            print(f"[openai/thinking+tools] answer: {event.final_text or '(no text)'}")
            print(f"[openai/thinking+tools] total thinking chars: {thinking_chars}")
        elif event.type == "error":
            print(f"[openai/thinking+tools] ERROR: {event.error.get('message', '')}")


# ── 5. Anthropic path: basic Q&A ─────────────────────────────────────────────


async def run_anthropic_basic(api_key: str) -> None:
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools

    agent = Agent(
        model=MODEL_FLASH,
        provider=_make_anthropic_provider(api_key),
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
            print(f"[anthropic/basic] {event.final_text}")
        elif event.type == "error":
            print(f"[anthropic/basic] ERROR: {event.error.get('message', '')}")


# ── 6. Anthropic path: thinking enabled ──────────────────────────────────────


async def run_anthropic_thinking(api_key: str) -> None:
    """DeepSeek reasoning via the Anthropic-compatible endpoint with thinking."""
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.providers.anthropic import AnthropicProvider, AnthropicProviderOptions
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools

    provider = AnthropicProvider(
        AnthropicProviderOptions(
            api_key=api_key,
            base_url=DEEPSEEK_BASE_ANTHROPIC,
            thinking={"type": "enabled", "budget_tokens": 2000},
        )
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
        include_partial_messages=True,
    )
    session = await agent.session()

    thinking_chars = 0
    async for event in session.run("What is 17 × 23? Show your reasoning."):
        if event.type == "partial_assistant":
            if event.delta.get("kind") == "thinking":
                thinking_chars += len(event.delta.get("text", ""))
        elif event.type == "result":
            print(f"[anthropic/thinking] answer: {event.final_text}")
            print(f"[anthropic/thinking] thinking chars streamed: {thinking_chars}")
        elif event.type == "error":
            print(f"[anthropic/thinking] ERROR: {event.error.get('message', '')}")


# ── 7. Multi-turn context preserved across turns ──────────────────────────────


async def run_multi_turn(api_key: str) -> None:
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools

    agent = Agent(
        model=MODEL_FLASH,
        provider=_make_openai_provider(api_key),
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

    print("=== 1. OpenAI path — basic Q&A ===")
    await run_openai_basic(api_key)

    print("\n=== 2. OpenAI path — thinking events visible ===")
    await run_openai_thinking(api_key)

    print("\n=== 3. OpenAI path — tool use + multi-turn ===")
    await run_openai_tools(api_key)

    print("\n=== 4. OpenAI path — thinking + tool use ===")
    await run_openai_thinking_with_tools(api_key)

    print("\n=== 5. Anthropic path — basic Q&A ===")
    await run_anthropic_basic(api_key)

    print("\n=== 6. Anthropic path — thinking enabled ===")
    await run_anthropic_thinking(api_key)

    print("\n=== 7. Multi-turn context (OpenAI path) ===")
    await run_multi_turn(api_key)


if __name__ == "__main__":
    asyncio.run(main())
