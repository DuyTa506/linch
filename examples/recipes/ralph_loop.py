"""The Ralph loop — brute-force long-horizon execution over a fresh context.

Run:
    OPENAI_API_KEY=sk-...    python examples/recipes/ralph_loop.py
    DEEPSEEK_API_KEY=sk-...  python examples/recipes/ralph_loop.py

The "Ralph loop" (Geoffrey Huntley) is a long-horizon pattern: instead of keeping
one ever-growing conversation alive and compacting it, you run the agent in an
*outer loop* where **each iteration gets a fresh context**, reads the **same spec**,
makes a little progress, and writes its state back to **the filesystem** — which is
the only memory carried between iterations. The thesis is an acceptance of model
fallibility: don't engineer a flawless super-agent, just loop with persistence until
the work converges.

This is a *harness pattern you compose*, not a core-loop feature — it is plain
embedder code over seams the SDK already provides:

  - ``Agent(filesystem=...)``   — registers ls/read_file/write_file/edit_file; the
                                  backend is the disk-as-memory that survives a fresh
                                  context (here ``StateFileBackend``; swap in
                                  ``DiskFileBackend`` for real files + git).
  - ``await agent.session()``   — a brand-new context every iteration (the opposite
                                  of compaction: discard and restart, don't summarize).
  - a **done-predicate**        — the embedder reads the same backend to decide when
                                  to stop; ``max_iterations`` bounds the spend.

Contrast with linch's other long-horizon tool: ``CompactionLadder`` keeps a single
session alive and summarizes when the window fills. Ralph throws the window away each
pass. Both solve "work exceeds one context window" — opposite philosophies.

``build_ralph_agent`` is a factory so the smoke test in
``tests/test_example_ralph_loop.py`` can drive it with a deterministic ``ScriptedProvider``.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from linch import Agent, empty_tools
from linch.config import FeatureFlags, SystemPromptConfig
from linch.filesystem import StateFileBackend
from linch.sessions import InMemorySessionStore

PROGRESS_PATH = "/progress.md"
DONE_MARKER = "ALL DONE"

# The fixed spec, re-read verbatim every iteration — the heart of Ralph: the prompt
# never changes; only the filesystem state does.
SPEC = f"""You are completing a multi-step task one increment per run, across many runs.

Your memory is the file {PROGRESS_PATH}. Each run:
  1. read {PROGRESS_PATH} (it may not exist yet — then start the checklist).
  2. do exactly ONE unredone item.
  3. write {PROGRESS_PATH} back with that item checked off.
  4. when every item is checked, append the line "{DONE_MARKER}".

The checklist is: [ ] scaffold [ ] implement [ ] test. Do one item, then stop."""


def build_ralph_agent(
    *, provider: Any = None, model: str | None = None, backend: Any = None
) -> tuple[Agent, Any]:
    """Build an agent whose only memory is a virtual filesystem backend.

    Pass ``provider`` + ``model`` (e.g. a ``ScriptedProvider``) to drive it
    deterministically. ``backend`` defaults to an in-memory ``StateFileBackend``;
    it is shared with the embedder so the done-predicate reads what the agent wrote.
    Returns ``(agent, backend)``.
    """
    backend = backend or StateFileBackend()
    kwargs: dict[str, Any] = {}
    if provider is not None:
        kwargs["provider"] = provider

    agent = Agent(
        model=model or "ralph-demo",
        # Scope tools to the virtual filesystem only: with skip-dangerous
        # auto-approval in an UNATTENDED loop, the disk-as-memory backend must be
        # the agent's only reach — empty_tools() drops the default Bash/Write/Edit/
        # Read (which would otherwise run on the real cwd), and filesystem=backend
        # then auto-registers ls/read_file/write_file/edit_file over the backend.
        # (A "real" Ralph that edits a repo + commits to git would instead keep the
        # default tools and run sandboxed — never with skip-dangerous on a live box.)
        tools=empty_tools(),
        filesystem=backend,
        system_prompt_config=SystemPromptConfig(replace_defaults=True, append=SPEC),
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        **kwargs,
    )
    return agent, backend


async def _is_done(backend: Any) -> bool:
    """The done-predicate: read disk-as-memory, stop when the marker appears."""
    if not await backend.exists(PROGRESS_PATH):
        return False
    return DONE_MARKER in await backend.read(PROGRESS_PATH)


async def run_ralph_loop(
    agent: Agent,
    backend: Any,
    *,
    spec: str = SPEC,
    max_iterations: int = 20,
    on_iteration: Any = None,
) -> dict[str, Any]:
    """Loop the agent over *spec* until ``_is_done`` or ``max_iterations``.

    Each pass runs in a **fresh session** (a clean context); the only thing carried
    between passes is whatever the agent persisted to ``backend``. Returns
    ``{"iterations": n, "done": bool}``.
    """
    for i in range(1, max_iterations + 1):
        session = await agent.session()  # fresh context — the Ralph move
        try:
            final_text = ""
            async for event in session.run(spec):
                if event.type == "result":
                    final_text = event.final_text or ""
        finally:
            # Drop the finished session so a long loop doesn't accumulate one per
            # pass. There is no public per-session dispose yet (agent.close() ends
            # the whole agent), so this mirrors the SDK's own internal pattern
            # (workflow/engine.py, evals/harness.py). Safe: nothing else holds it.
            agent._sessions.pop(session.id, None)

        if on_iteration is not None:
            on_iteration(i, final_text)
        if await _is_done(backend):
            return {"iterations": i, "done": True}
    return {"iterations": max_iterations, "done": False}


async def main() -> None:
    from linch.providers import OpenAIChatCompletionsProvider, OpenAIChatProviderOptions

    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        print("Set OPENAI_API_KEY or DEEPSEEK_API_KEY to run this example.")
        return

    base_url = "https://api.deepseek.com" if os.environ.get("DEEPSEEK_API_KEY") else None
    provider = OpenAIChatCompletionsProvider(
        OpenAIChatProviderOptions(api_key=api_key, base_url=base_url)
    )
    agent, backend = build_ralph_agent(provider=provider, model="gpt-4o-mini")

    def report(i: int, text: str) -> None:
        print(f"  iteration {i}: {text[:80]}")

    print("→ Running the Ralph loop (fresh context each pass, filesystem as memory)...")
    result = await run_ralph_loop(agent, backend, max_iterations=8, on_iteration=report)

    print(f"\nResult: {result}")
    if await backend.exists(PROGRESS_PATH):
        print(f"\nFinal {PROGRESS_PATH}:\n{await backend.read(PROGRESS_PATH)}")
    await agent.close()


if __name__ == "__main__":
    asyncio.run(main())
