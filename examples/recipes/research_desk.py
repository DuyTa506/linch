"""Research desk — a NON-CODING agent recipe.

Run:
    OPENAI_API_KEY=sk-...   python examples/recipes/research_desk.py
    DEEPSEEK_API_KEY=sk-...  python examples/recipes/research_desk.py

Linch is a *mechanism* SDK: nothing in the core loop is coding-shaped. This recipe
proves it by building a literature-research analyst that never touches a file or a
shell. It exercises the same seams a coding agent uses, on a domain made of
articles and citations:

  - Domain function tools          — search_library / read_article / record_citation
                                     (read tools run in parallel; the write tool serializes)
  - ctx.deps                       — a shared in-memory corpus + a citations ledger,
                                     swappable per run (no module globals)
  - OutputSchema                   — a structured research Brief (summary + sources)
  - A closed-loop Verifier         — bounces the answer back until it cites a source,
                                     wired via FinalAnswerVerifierHook (the hooks layer)

The agent is built by a factory so the smoke test in
``tests/test_example_research_desk.py`` can drive it with a deterministic
``ScriptedProvider`` — no live key required for CI. ``main()`` runs it live.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from linch import (
    Agent,
    FinalAnswerVerifierHook,
    OutputSchema,
    Verdict,
)
from linch.config import FeatureFlags, SystemPromptConfig
from linch.sessions import InMemorySessionStore
from linch.tools import tool
from linch.tools.base import ToolContext
from linch.tools.registry import empty_tools

MODEL = os.environ.get("RESEARCH_MODEL", "gpt-5-nano-2025-08-07")


# ── A tiny in-memory "library" the analyst can search and read ───────────────
# In production this is a vector store / search API. Here it's plain data so the
# recipe runs offline.

LIBRARY: dict[str, dict[str, str]] = {
    "ART-001": {
        "title": "Sleep duration and memory consolidation",
        "body": "A meta-analysis of 38 studies found that 7-9 hours of sleep improved "
        "recall by 23% versus restricted sleep. Slow-wave sleep was the key phase.",
    },
    "ART-002": {
        "title": "Caffeine timing and alertness",
        "body": "Caffeine taken within 6 hours of bedtime reduced total sleep time by "
        "41 minutes on average and fragmented slow-wave sleep.",
    },
    "ART-003": {
        "title": "Exercise and sleep quality",
        "body": "Moderate aerobic exercise improved sleep-onset latency, but vigorous "
        "exercise within 1 hour of bedtime delayed it.",
    },
}


# ── Domain tools (no files, no shell) ────────────────────────────────────────


@tool(
    description="Search the research library; returns matching article ids and titles.",
    scope="read",
    parallel=True,
    summary=lambda input: f"search_library({input.get('query', '?')[:32]})",
)
async def search_library(query: str, ctx: ToolContext) -> str:
    library: dict[str, dict[str, str]] = ctx.deps["library"]
    q = query.lower()
    hits = [
        f"{aid}: {art['title']}"
        for aid, art in library.items()
        if any(word in art["title"].lower() or word in art["body"].lower() for word in q.split())
    ]
    return "\n".join(hits) if hits else "No matching articles."


@tool(
    description="Read the full text of an article by its id (e.g. ART-001).",
    scope="read",
    parallel=True,
    summary=lambda input: f"read_article({input.get('article_id', '?')})",
)
async def read_article(article_id: str, ctx: ToolContext) -> str:
    art = ctx.deps["library"].get(article_id)
    if art is None:
        return f"No article {article_id}."
    return f"{art['title']}\n\n{art['body']}"


@tool(
    description="Record a citation: an article id and the claim it supports.",
    scope="write",
    parallel=False,
    summary=lambda input: f"record_citation({input.get('article_id', '?')})",
)
async def record_citation(article_id: str, claim: str, ctx: ToolContext) -> str:
    ctx.deps["citations"].append({"article_id": article_id, "claim": claim})
    return f"Recorded citation to {article_id}."


# ── A closed-loop verifier: refuse an uncited brief ──────────────────────────
#
# A Verifier is duck-typed: a `name` plus `verify(ctx) -> Verdict`. This one
# shares the run's citation ledger by reference and bounces the answer back
# (action="retry") with feedback until at least one source is recorded.


class CitedSourcesVerifier:
    name = "cited-sources"

    def __init__(self, citations: list[dict[str, str]]) -> None:
        self._citations = citations

    def verify(self, ctx: Any) -> Verdict:
        if self._citations:
            return Verdict(action="pass")
        return Verdict(
            action="retry",
            feedback=(
                "Your brief cites no sources. Use read_article to verify a claim, then "
                "record_citation(article_id, claim) for at least one source before answering."
            ),
            reason="no citations recorded",
        )


BRIEF_SCHEMA = OutputSchema(
    name="research_brief",
    schema={
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "2-3 sentence synthesis."},
            "sources": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Article ids cited, e.g. ['ART-001'].",
            },
        },
        "required": ["summary", "sources"],
    },
)


SYSTEM = (
    "You are a research analyst. Answer questions ONLY from the research library. "
    "Search the library, read the relevant articles, record a citation for each claim "
    "you make, then return a structured brief. Never invent findings."
)


def build_research_desk(
    *,
    provider: Any = None,
    api_key: str = "",
    model: str = MODEL,
) -> tuple[Agent, dict[str, Any]]:
    """Construct the research-desk agent and its per-run deps.

    Pass ``provider=`` a ``ScriptedProvider`` for offline/testing, or ``api_key=``
    for a live run. Returns ``(agent, deps)`` — inspect ``deps['citations']`` after
    a run to see what the analyst grounded its answer on.
    """
    deps: dict[str, Any] = {"library": LIBRARY, "citations": []}
    kwargs: dict[str, Any] = {}
    if provider is not None:
        kwargs["provider"] = provider
        kwargs["model"] = model
    else:
        kwargs["model"] = model
        kwargs["openai_api_key"] = api_key

    agent = Agent(
        tools=empty_tools(search_library, read_article, record_citation),
        deps=deps,
        system_prompt_config=SystemPromptConfig(replace_defaults=True, append=SYSTEM),
        hooks=[FinalAnswerVerifierHook([CitedSourcesVerifier(deps["citations"])])],
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        **kwargs,
    )
    return agent, deps


async def main() -> None:
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        print("Set OPENAI_API_KEY or DEEPSEEK_API_KEY to run this example.")
        return

    agent, deps = build_research_desk(api_key=api_key)
    session = await agent.session()
    question = "How does caffeine timing affect sleep and memory? Cite your sources."

    final: Any = None
    async for event in session.run(question, opts=_brief_opts()):
        if event.type == "tool_call_end":
            print(f"  · {event.tool_name}: {event.summary}")
        elif event.type == "result":
            final = event

    print("\nBrief:")
    brief = final.structured_output
    print(json.dumps(brief, indent=2) if brief else final.final_text)
    print("\nCitations grounded on:", deps["citations"])


def _brief_opts() -> Any:
    from linch import RunOptions

    return RunOptions(output_schema=BRIEF_SCHEMA)


if __name__ == "__main__":
    asyncio.run(main())
