"""Compaction ladder — cheap recovery rungs before LLM summarization.

``Agent(compaction_ladder=CompactionLadder())`` adds two recovery rungs:

1. **micro-compact** — elide old tool-result contents (no LLM call), both
   proactively near the context-window threshold and reactively when the
   provider raises ``ContextLengthError``.
2. **forced compaction** — the existing LLM summarization, now capped by a
   per-run circuit breaker (``max_forced_compactions``).

Offline demo: a fake provider raises ``ContextLengthError`` on one turn, then
succeeds — watch the ``compaction`` events show the rungs firing.
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from linch import Agent, CompactionLadder, DetailedCompaction
from linch.errors import ContextLengthError
from linch.sessions import InMemorySessionStore
from linch.tools import ToolRegistry, ToolResult
from linch.types import TextBlock, ToolResultBlock, Usage


class NoisyTool:
    """A read tool that returns a large payload."""

    name = "NoisyTool"
    description = "Return a large filler payload."
    input_schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
    scope = "read"
    parallel = True

    def validate(self, raw: dict[str, object]) -> dict[str, object]:
        return raw

    def summarize(self, input: dict[str, object]) -> str:
        return f"NoisyTool({input.get('n')})"

    async def execute(self, input: dict[str, object], ctx: Any) -> ToolResult:
        n = int(input.get("n", 1000))  # type: ignore[arg-type]
        return ToolResult(content="x" * n, summary=f"{n} chars")


class OverflowingProvider:
    """Two tool turns, then a turn that overflows twice before succeeding."""

    id = "fake"

    def __init__(self) -> None:
        self.behaviors = ["tool", "tool", "raise", "raise", "text"]
        self.calls = 0

    def context_window(self, model: str) -> int:
        return 1_000_000

    async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
        if not req.tools:  # summarization side-call from forced compaction
            yield {"type": "message_start", "model": req.model}
            yield {"type": "text_delta", "text": "summary of earlier work"}
            yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}
            return
        behavior = self.behaviors[self.calls]
        self.calls += 1
        if behavior == "raise":
            raise ContextLengthError("prompt too long")
        yield {"type": "message_start", "model": req.model}
        if behavior == "tool":
            yield {"type": "tool_use_start", "id": f"c{self.calls}", "name": "NoisyTool"}
            yield {"type": "tool_use_input_delta", "id": f"c{self.calls}", "json_delta": "{}"}
            yield {"type": "tool_use_end", "id": f"c{self.calls}"}
            stop = "tool_use"
        else:
            yield {"type": "text_delta", "text": "done"}
            stop = "end_turn"
        yield {"type": "message_end", "stop_reason": stop, "usage": Usage(input_tokens=10)}


def char_estimator(messages: list[Any], model: str) -> int:
    """Count tool-result chars too, so micro-compact savings are visible."""
    total = 0
    for message in messages:
        for block in message.content:
            if isinstance(block, TextBlock):
                total += len(block.text)
            elif isinstance(block, ToolResultBlock) and isinstance(block.content, str):
                total += len(block.content)
    return total // 4


async def main() -> None:
    tools = ToolRegistry()
    tools.register(NoisyTool())

    agent = Agent(
        model="gpt-5",
        provider=OverflowingProvider(),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        tools=tools,
        compaction=DetailedCompaction(keep_recent_turns=1),
        token_estimator=char_estimator,
        # The ladder: elide old tool results first; at most 3 LLM compactions
        # per run before the ContextLengthError surfaces.
        compaction_ladder=CompactionLadder(keep_recent_turns=1, max_forced_compactions=3),
    )
    session = await agent.session()

    async for event in session.run("explore"):
        if event.type == "compaction":
            print(
                f"[compaction] strategy={event.strategy} "
                f"tokens {event.tokens_before} -> {event.tokens_after}"
            )
        elif event.type == "result":
            print(f"[result] subtype={event.subtype}")

    await agent.close()


if __name__ == "__main__":
    asyncio.run(main())
