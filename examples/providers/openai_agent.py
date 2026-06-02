"""OpenAI Chat Completions provider examples.

Run:
    OPENAI_API_KEY=sk-... python3 examples/providers/openai_agent.py

Requires `pip install openai`.

Demonstrates:
  1. Basic Q&A — no tools.
  2. Thinking events — reasoning_content streamed (for models that emit it).
  3. Tool use — Bash tool, multi-turn.
  4. Thinking + tool use — reasoning captured before each tool call.
  5. Structured output — JSON Schema response format.
  6. Multi-turn context preserved across turns.

Provider note:
  OpenAIChatCompletionsProvider targets the standard Chat Completions endpoint.
  DeepSeek and any OpenAI-compatible provider (Azure, Together, Groq, …) use
  the same class — just pass a custom base_url via OpenAIChatProviderOptions.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Override this with whatever model you have access to.
MODEL = os.environ.get("OPENAI_MODEL", "gpt-5-nano-2025-08-07")


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


def _make_provider(api_key: str) -> "OpenAIChatCompletionsProvider":
    from linch.providers import OpenAIChatCompletionsProvider
    from linch.providers.openai_chat import OpenAIChatProviderOptions

    return OpenAIChatCompletionsProvider(OpenAIChatProviderOptions(api_key=api_key))


# ── 1. Basic Q&A ──────────────────────────────────────────────────────────────


async def run_basic(api_key: str) -> None:
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools

    agent = Agent(
        model=MODEL,
        provider=_make_provider(api_key),
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
            print(f"[openai/basic] {event.final_text}")
        elif event.type == "error":
            print(f"[openai/basic] ERROR: {event.error.get('message', '')}")


# ── 2. Thinking events (for models that emit reasoning_content) ───────────────


async def run_thinking(api_key: str) -> None:
    """Stream reasoning_content as thinking events.

    Models that support chain-of-thought reasoning (e.g. o-series, deepseek-
    reasoner, or any model that emits reasoning_content in Chat Completions
    streaming) will produce PartialAssistantEvent with kind="thinking".
    Standard GPT models skip this block gracefully (thinking_chars stays 0).
    """
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools

    agent = Agent(
        model=MODEL,
        provider=_make_provider(api_key),
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


# ── 3. Tool use + multi-turn ──────────────────────────────────────────────────


async def run_tools(api_key: str) -> None:
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.loop_guard import LoopGuard
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import tools_from_defaults

    agent = Agent(
        model=MODEL,
        provider=_make_provider(api_key),
        tools=tools_from_defaults(),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        loop_guard=LoopGuard(max_identical_tool_calls=3, force_final_answer=True),
        system_prompt="Be concise. Use the Bash tool to run shell commands.",
    )
    session = await agent.session()

    async for event in session.run("Run: echo hello-openai  and show me the output."):
        if event.type == "tool_call_end":
            print(f"[openai/tools] tool: {event.tool_name} → {str(event.result)[:60]!r}")
        elif event.type == "result":
            print(f"[openai/tools] turn 1: {event.final_text or '(no text)'}")
        elif event.type == "error":
            print(f"[openai/tools] ERROR: {event.error.get('message', '')}")

    async for event in session.run("Now run: echo second-turn"):
        if event.type == "tool_call_end":
            print(f"[openai/tools] tool: {event.tool_name} → {str(event.result)[:60]!r}")
        elif event.type == "result":
            print(f"[openai/tools] turn 2: {event.final_text or '(no text)'}")
        elif event.type == "error":
            print(f"[openai/tools] ERROR: {event.error.get('message', '')}")


# ── 4. Thinking + tool use ────────────────────────────────────────────────────


async def run_thinking_with_tools(api_key: str) -> None:
    """Reasoning visible while the model drives tool loops.

    If the model emits reasoning_content, thinking events appear before each
    tool call.  The ThinkingBlock is round-tripped so subsequent turns see the
    prior reasoning context (deepseek-v4-flash style).  Standard GPT models
    that don't emit reasoning_content just skip thinking events silently.
    """
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.loop_guard import LoopGuard
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import tools_from_defaults

    agent = Agent(
        model=MODEL,
        provider=_make_provider(api_key),
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
    async for event in session.run('Run: python3 -c "print(6*7)" and tell me the result.'):
        if event.type == "partial_assistant":
            if event.delta.get("kind") == "thinking":
                thinking_chars += len(event.delta.get("text", ""))
        elif event.type == "tool_call_end":
            print(f"[openai/thinking+tools] tool: {event.tool_name} → {str(event.result)[:60]!r}")
        elif event.type == "result":
            print(f"[openai/thinking+tools] answer: {event.final_text or '(no text)'}")
            print(f"[openai/thinking+tools] thinking chars: {thinking_chars}")
        elif event.type == "error":
            print(f"[openai/thinking+tools] ERROR: {event.error.get('message', '')}")


# ── 5. Structured output ──────────────────────────────────────────────────────


async def run_structured(api_key: str) -> None:
    """JSON Schema response format — parsed dict in ResultEvent.structured_output."""
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools
    from linch.types import OutputSchema

    agent = Agent(
        model=MODEL,
        provider=_make_provider(api_key),
        tools=empty_tools(),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        loop_guard=None,
        system_prompt="Respond only with the requested JSON.",
        output_schema=OutputSchema(
            name="city_info",
            schema={
                "type": "object",
                "properties": {
                    "city": {"type": "string"},
                    "country": {"type": "string"},
                    "population_million": {"type": "number"},
                },
                "required": ["city", "country", "population_million"],
                "additionalProperties": False,
            },
        ),
    )
    session = await agent.session()
    async for event in session.run("Give me info about Tokyo."):
        if event.type == "result":
            if event.structured_output:
                print(f"[openai/structured] {event.structured_output}")
            else:
                print(f"[openai/structured] raw: {event.final_text}")
                if event.structured_error:
                    print(f"[openai/structured] parse error: {event.structured_error}")
        elif event.type == "error":
            print(f"[openai/structured] ERROR: {event.error.get('message', '')}")


# ── 6. Multi-turn context ─────────────────────────────────────────────────────


async def run_multi_turn(api_key: str) -> None:
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools

    agent = Agent(
        model=MODEL,
        provider=_make_provider(api_key),
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
                print(f"[openai/multi-turn] Q: {prompt!r:50s}  A: {event.final_text}")


# ── Entry point ────────────────────────────────────────────────────────────────


async def main() -> None:
    load_project_env()
    # The api_key.txt uses OPENAPI_KEY (non-standard); fall back to OPENAI_API_KEY.
    api_key = os.environ.get("OPENAPI_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("Set OPENAI_API_KEY (or OPENAPI_KEY) and re-run.")
        return

    print(f"Using model: {MODEL}\n")

    print("=== 1. Basic Q&A ===")
    await run_basic(api_key)

    print("\n=== 2. Thinking events (reasoning_content if supported) ===")
    await run_thinking(api_key)

    print("\n=== 3. Tool use + multi-turn ===")
    await run_tools(api_key)

    print("\n=== 4. Thinking + tool use ===")
    await run_thinking_with_tools(api_key)

    print("\n=== 5. Structured output ===")
    await run_structured(api_key)

    print("\n=== 6. Multi-turn context ===")
    await run_multi_turn(api_key)


if __name__ == "__main__":
    asyncio.run(main())
