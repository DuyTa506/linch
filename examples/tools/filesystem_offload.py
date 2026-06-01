"""Filesystem offload — keep large tool results out of the context window.

Variable-length tool results (web_search, RAG, big file dumps) are the #1 cause
of context-window blowup.  AgentKit's virtual filesystem subsystem handles this
the way Deep Agents does: when a tool returns more than ``threshold_tokens``,
the scheduler writes the full payload to a ``FileBackend`` and replaces what the
model sees with a short preview + a path.  The model pulls back only what it
needs via the ``read_file`` / ``ls`` tools.

This example has two parts:

1. ``demo_offline()`` — no API key needed.  Shows the mechanics directly:
   backends, the four tools, and the offload transform.
2. ``demo_live()`` — needs ANTHROPIC_API_KEY.  Runs a real agent whose search
   tool returns a huge result, and prints the events so you can watch the
   offload + read-back happen.

Backends shown:
  - StateFileBackend     — ephemeral, in-memory, one per session (default).
  - DiskFileBackend      — real files under .agent_kit/offload (inspectable,
                           but kept OUT of your working dir / repo).
  - CompositeFileBackend — route /memories/ to a persistent SQLite store,
                           everything else to ephemeral state.

Run:
    python examples/tools/filesystem_offload.py
    ANTHROPIC_API_KEY=sk-... python examples/tools/filesystem_offload.py
"""

from __future__ import annotations

import asyncio
import os

from agent_kit import Agent
from agent_kit.config import SystemPromptConfig
from agent_kit.filesystem import (
    CompositeFileBackend,
    DiskFileBackend,
    OffloadConfig,
    SqliteFileBackend,
    StateFileBackend,
)
from agent_kit.filesystem.offload import maybe_offload
from agent_kit.sessions import InMemorySessionStore
from agent_kit.tools.base import ToolContext, ToolResult
from agent_kit.tools.registry import tools_from_defaults

# ── A tool that returns a large result (e.g. a verbose web/RAG search) ────────


class BigSearchTool:
    name = "big_search"
    description = "Search the corpus. Returns many verbose results."
    input_schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }
    scope = "read"
    parallel_safe = True
    parallel = True

    def validate(self, raw: dict) -> dict:
        if not raw.get("query"):
            raise ValueError("query is required")
        return {"query": str(raw["query"])}

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        # Simulate a big payload: 300 "chunks" of text.
        chunks = [
            f"[doc-{i}] Result for '{input['query']}': "
            f"lorem ipsum dolor sit amet, consectetur adipiscing elit, "
            f"sed do eiusmod tempor incididunt ut labore et dolore magna."
            for i in range(300)
        ]
        return ToolResult(content="\n".join(chunks), summary=f"big_search({input['query']!r})")

    def summarize(self, input: dict) -> str:
        return f"big_search({input.get('query', '?')!r})"


# ── Part 1: offline mechanics (no API key) ───────────────────────────────────


async def demo_offline() -> None:
    print("\n── Demo 1: offload mechanics (no API key) ──")

    # The three backend flavours, same protocol.
    state = StateFileBackend()
    disk = DiskFileBackend(root=".agent_kit/offload")  # real files, but tucked away
    composite = CompositeFileBackend(
        default=StateFileBackend(),
        routes={"/memories/": SqliteFileBackend(":memory:")},
    )

    big = "\n".join(f"line {i}: some verbose tool output" for i in range(500))
    print(f"Original result: {len(big)} chars (~{len(big) // 4} tokens)")

    for name, backend in [("state", state), ("disk", disk), ("composite", composite)]:
        result = ToolResult(content=big)
        out = await maybe_offload(
            result,
            tool_name="big_search",
            call_id=f"demo-{name}",
            backend=backend,
            config=OffloadConfig(threshold_tokens=50, preview_lines=3),
        )
        path = out.metadata["offloaded_to"]
        recovered = await backend.read(path)
        # Read back just lines 10-12 the way the model would.
        window = await backend.read(path, offset=10, limit=3)
        print(f"\n  [{name}] model now sees {len(out.content)} chars; full text at {path}")
        print(f"  [{name}] read_file(offset=10, limit=3):\n    " + window.replace("\n", "\n    "))
        assert recovered == big  # nothing lost

    # CompositeFileBackend: /memories/ persists, everything else is ephemeral.
    await composite.write("/memories/user_pref.txt", "prefers concise answers")
    await composite.write("/scratch/tmp.txt", "throwaway")
    print("\n  composite ls():", await composite.ls())


# ── Part 2: live agent (needs ANTHROPIC_API_KEY) ─────────────────────────────


async def demo_live() -> None:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("\n── Demo 2 skipped (set ANTHROPIC_API_KEY to run the live agent) ──")
        return

    print("\n── Demo 2: live agent with auto-offload ──")
    from agent_kit.providers.anthropic import AnthropicProvider, AnthropicProviderOptions

    agent = Agent(
        model="claude-sonnet-4-6",
        provider=AnthropicProvider(AnthropicProviderOptions(api_key=api_key)),
        system_prompt_config=SystemPromptConfig(
            replace_defaults=True,
            append=(
                "You are a research assistant. Use big_search to find information. "
                "Large results are offloaded to the virtual filesystem — use read_file "
                "to inspect the parts you need, then answer concisely."
            ),
        ),
        tools=tools_from_defaults(extra=[BigSearchTool()]),
        # Offload anything over ~200 tokens to real files under .agent_kit/offload.
        filesystem=DiskFileBackend(root=".agent_kit/offload"),
        result_offload=OffloadConfig(threshold_tokens=200, preview_lines=5),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
    )
    session = await agent.session()
    async for event in session.run(
        "Search for 'context engineering' and summarize the top findings."
    ):
        if event.type == "tool_call_start":
            print(f"  → {event.summary}")
        elif event.type == "tool_call_end":
            preview = event.result[:80].replace("\n", " ")
            offloaded = event.tool_result and event.tool_result.metadata.get("offloaded_to")
            tag = f"  [offloaded → {offloaded}]" if offloaded else ""
            print(f"  ← {event.tool_name}: {preview}…{tag}")
        elif event.type == "result":
            print("\nAnswer:", event.final_text)


async def main() -> None:
    await demo_offline()
    await demo_live()


if __name__ == "__main__":
    asyncio.run(main())
