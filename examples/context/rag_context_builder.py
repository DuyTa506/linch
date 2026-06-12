"""RAG context with the first-class ContextBuilder API.

Run:
    python3 examples/rag_context_builder.py

This example loads ../.env automatically when present. It does not print any
secret values. The local demo runs without a provider key.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path

from linch import Agent, ContextBudget, ContextBuildResult, ContextBuildTurn
from linch.context import apply_context_budget
from linch.hooks import ContextInjectionHook
from linch.sessions import InMemorySessionStore
from linch.tools.registry import empty_tools
from linch.types import Message, TextBlock

ROOT = Path(__file__).resolve().parents[1]
MODEL = "gpt-5-nano-2025-08-07"
TAG = "[[rag-context]]"


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


@dataclass(slots=True)
class ContextReport:
    query: str
    selected_ids: list[str]
    budget_tokens: int
    used_tokens: int = 0
    metadata: dict[str, object] = field(default_factory=dict)


class KeywordRagBuilder:
    def __init__(self, docs: dict[str, str], *, budget_tokens: int = 120) -> None:
        self.docs = docs
        self.budget_tokens = budget_tokens
        self.last_report: ContextReport | None = None

    async def build(self, turn: ContextBuildTurn) -> ContextBuildResult:
        query = last_user_text(turn.messages).lower()
        scored: list[tuple[int, str, str]] = []
        for doc_id, text in self.docs.items():
            score = sum(1 for word in query.split() if word in text.lower())
            if score:
                scored.append((score, doc_id, text))
        scored.sort(reverse=True)

        blocks = [f"[{doc_id}] {text}" for _score, doc_id, text in scored]
        selected_ids = [doc_id for _score, doc_id, _text in scored]
        self.last_report = ContextReport(
            query=query,
            selected_ids=selected_ids,
            budget_tokens=self.budget_tokens,
            metadata={"turn_index": turn.turn_index},
        )
        if not blocks:
            return ContextBuildResult(metadata={"rag_hits": 0})

        return ContextBuildResult(
            messages=[
                Message(
                    role="user",
                    content=[TextBlock(text=TAG + "\nRelevant context:\n" + "\n".join(blocks))],
                )
            ],
            budget=ContextBudget(max_tokens=self.budget_tokens),
            metadata={"rag_hits": len(blocks), "selected_ids": selected_ids},
        )


def last_user_text(messages: list[Message]) -> str:
    for message in reversed(messages):
        if message.role != "user":
            continue
        for block in message.content:
            if isinstance(block, TextBlock) and not block.text.startswith("<env>"):
                return block.text
    return ""


async def local_builder_demo() -> None:
    docs = {
        "d1": "Agent Kit schedulers run independent read tools in parallel.",
        "d2": "ContextBuilder output is ephemeral and does not grow session history.",
        "d3": "ContextBuildEvent reports metadata and budget usage to host apps.",
    }
    builder = KeywordRagBuilder(docs, budget_tokens=120)
    turn = ContextBuildTurn(
        session=None,  # type: ignore[arg-type]
        messages=[
            Message(
                role="user",
                content=[TextBlock(text="How should RAG context avoid growing forever?")],
            )
        ],
        turn_index=0,
        deps=None,
        model=MODEL,
        tools=empty_tools(),
    )
    result = apply_context_budget(await builder.build(turn), model=MODEL)
    if builder.last_report:
        builder.last_report.used_tokens = result.budget.used_tokens
    print("Context messages:", len(result.messages))
    print("Budget:", result.budget)
    print("Report:", builder.last_report)


async def maybe_live_agent() -> None:
    load_project_env()
    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY not set; skipped live agent call.")
        return

    builder = KeywordRagBuilder(
        {
            "scheduler": "Agent Kit can run parallel read tools with resource guards.",
            "context": "ContextBuilder returns ephemeral request context each turn.",
            "results": "ToolResult can carry citations and metadata for host apps.",
        }
    )
    agent = Agent(
        model=MODEL,
        openai_api_key=os.environ.get("OPENAI_API_KEY"),
        tools=empty_tools(),
        hooks=[ContextInjectionHook(builder)],
        session_store=InMemorySessionStore(),
        system_prompt="Answer only from provided context when it is available.",
    )
    session = await agent.session()
    async for event in session.run("How does Agent Kit keep RAG context small?"):
        if event.type == "context_build":
            print("Context event:", event.metadata, event.budget)
        if event.type == "result":
            print("Live answer:", event.final_text)


async def main() -> None:
    await local_builder_demo()
    await maybe_live_agent()


if __name__ == "__main__":
    asyncio.run(main())
