"""ContextBuilder patterns: RAG, schema context, and selected tools.

The filename is kept for continuity with older examples. The legacy
context_hooks API has been removed; new code should use ContextBuilder.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from linch import Agent, ContextBudget, ContextBuildResult, ContextBuildTurn
from linch.sessions import InMemorySessionStore
from linch.tools import ToolContext, ToolResult
from linch.tools.registry import empty_tools
from linch.types import Message, SystemBlock, TextBlock

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


def last_user_text(messages: list[Message]) -> str:
    for message in reversed(messages):
        if message.role != "user":
            continue
        for block in message.content:
            if isinstance(block, TextBlock) and not block.text.startswith("<env>"):
                return block.text
    return ""


class ProfileContextBuilder:
    async def build(self, turn: ContextBuildTurn) -> ContextBuildResult:
        profile = turn.deps.get("profile", {}) if isinstance(turn.deps, dict) else {}
        if not profile:
            return ContextBuildResult()
        text = "User profile: " + ", ".join(f"{k}={v}" for k, v in sorted(profile.items()))
        return ContextBuildResult(
            system_blocks=[SystemBlock(text=text)],
            metadata={"profile_keys": sorted(profile)},
        )


class KnowledgeContextBuilder:
    async def build(self, turn: ContextBuildTurn) -> ContextBuildResult:
        kb = turn.deps.get("kb", {}) if isinstance(turn.deps, dict) else {}
        query = last_user_text(turn.messages).lower()
        hits = [text for key, text in kb.items() if key.lower() in query]
        if not hits:
            return ContextBuildResult(metadata={"kb_hits": 0})
        return ContextBuildResult(
            messages=[
                Message(
                    role="user",
                    content=[TextBlock(text="Relevant knowledge:\n" + "\n".join(hits))],
                )
            ],
            budget=ContextBudget(max_tokens=80),
            metadata={"kb_hits": len(hits)},
        )


class SearchOnlyContextBuilder:
    async def build(self, turn: ContextBuildTurn) -> ContextBuildResult:
        return ContextBuildResult(
            selected_tools={"LookupFact"},
            metadata={"selected_tools_reason": "demo limits provider schema to search"},
        )


class LookupFactTool:
    name = "LookupFact"
    description = "Lookup one fact by key."
    input_schema = {
        "type": "object",
        "properties": {"key": {"type": "string"}},
        "required": ["key"],
    }
    scope = "read"
    parallel = True
    tags = ("search",)

    def validate(self, raw):
        return {"key": str(raw.get("key", ""))}

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        facts = ctx.deps.get("facts", {}) if isinstance(ctx.deps, dict) else {}
        value = facts.get(input["key"], "not found")
        return ToolResult(content=value, summary=f"lookup({input['key']})")

    def summarize(self, input: dict) -> str:
        return f"lookup({input.get('key', '')})"


async def local_demo() -> None:
    builder = [ProfileContextBuilder(), KnowledgeContextBuilder(), SearchOnlyContextBuilder()]
    deps = {
        "profile": {"role": "analyst", "region": "APAC"},
        "kb": {"refund": "Refunds are available within 30 days."},
    }
    turn = ContextBuildTurn(
        session=None,  # type: ignore[arg-type]
        messages=[Message(role="user", content=[TextBlock(text="Explain the refund policy")])],
        turn_index=0,
        deps=deps,
        model=MODEL,
        tools=empty_tools(LookupFactTool()),
    )
    results = [await item.build(turn) for item in builder]
    print("System blocks:", sum(len(result.system_blocks) for result in results))
    print("Context messages:", sum(len(result.messages) for result in results))
    print("Selected tools:", results[-1].selected_tools)


async def maybe_live_agent() -> None:
    load_project_env()
    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY not set; skipped live agent call.")
        return

    agent = Agent(
        model=MODEL,
        openai_api_key=os.environ.get("OPENAI_API_KEY"),
        tools=empty_tools(LookupFactTool()),
        context_builder=[
            ProfileContextBuilder(),
            KnowledgeContextBuilder(),
            SearchOnlyContextBuilder(),
        ],
        deps={
            "profile": {"role": "analyst", "region": "APAC"},
            "kb": {"refund": "Refunds are available within 30 days."},
            "facts": {"refund": "Refunds are available within 30 days."},
        },
        session_store=InMemorySessionStore(),
    )
    session = await agent.session()
    async for event in session.run("What is the refund policy?"):
        if event.type == "context_build":
            print("Context:", event.metadata)
        if event.type == "result":
            print("Answer:", event.final_text)


async def main() -> None:
    await local_demo()
    await maybe_live_agent()


if __name__ == "__main__":
    asyncio.run(main())
