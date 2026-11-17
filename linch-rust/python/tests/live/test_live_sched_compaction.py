"""Live: compaction of a long session, and the session reloading on a new
server. Skips without the live environment.

The mechanics (a logged summary, a smaller provider view, a reload from
Postgres) are asserted for every provider. Recalling early content from the
summary (a code word given in the first turns must be answered after
compaction) is asserted too: the summary call ends with an instruction message,
so the model summarizes instead of answering the last user message
(`summary_request` in `crates/linch-core/src/compaction.rs`).
"""

from __future__ import annotations

from typing import Any

import pytest
from live_sched_support import kind, live_config, pg_dsn, seqs_increasing

from linch_rust import (
    DETAILED_COMPACTION_PROMPT,
    Agent,
    CompactionEvent,
    ContextBuildEvent,
    ResultEvent,
    Server,
)

__all__ = ["kind", "pg_dsn"]

pytestmark = pytest.mark.live

CODE = "PELICAN-7421"


def filler(n: int) -> str:
    """About 500 tokens of distinct text, so the view grows each turn."""
    words = " ".join(f"log{n}-{i}:ok" for i in range(220))
    return f"FYI, here is log excerpt {n}, no action needed: {words}."


def compacting(kind: str, dsn: str) -> Any:
    return live_config(
        kind,
        "compact",
        dsn,
        spec={
            "max_output_tokens": 300,
            "compaction": {
                "threshold_tokens": 2500,
                "keep_recent_turns": 2,
                "prompt": DETAILED_COMPACTION_PROMPT,
                "max_output_tokens": 800,
            },
        },
    )


async def fill(agent: Agent) -> list[str]:
    """A long session whose second message holds the code word (the first
    message stays verbatim after a summary, so only a summary can carry it)."""
    texts = [
        "Hello, I am setting up a deployment today.",
        f"For your records: the secret code word is {CODE}. Please keep it in mind.",
        *(filler(n) for n in range(1, 8)),
    ]
    run_ids = []
    for text in texts:
        run = await agent.run("long", text)
        assert isinstance(await run.result(), ResultEvent)
        run_ids.append(run.id)
    return run_ids


async def test_a_long_session_compacts_and_the_view_shrinks(kind: str, pg_dsn: str) -> None:
    config = compacting(kind, pg_dsn)
    async with Server() as server:
        agent = await server.open_agent(config)
        run_ids = await fill(agent)
        compactions: list[CompactionEvent] = []
        views: list[int] = []
        for run_id in run_ids:
            events = await agent.events(run_id)
            assert seqs_increasing(events)
            for envelope in events:
                if isinstance(envelope.event, CompactionEvent):
                    compactions.append(envelope.event)
                    views.append(-1)  # a marker: the next view is the compacted one
                elif isinstance(envelope.event, ContextBuildEvent):
                    views.append(envelope.event.messages)
        assert compactions, "the view crossed the threshold, so a summary was logged"
        summary = compactions[0]
        assert summary.strategy == "summary" and not summary.forced
        assert summary.messages_after < summary.messages_before
        assert summary.tokens_after < summary.tokens_before
        marker = views.index(-1)
        assert views[marker + 1] < max(views[:marker]), "the provider view shrank"

    # A new server loads the session from Postgres and goes on from the
    # compacted view (it does not replay the whole log into the model).
    async with Server() as server:
        agent = await server.open_agent(config)
        run = await agent.run("long", "Thanks. Reply with one word.")
        assert isinstance(await run.result(), ResultEvent)
        built = [
            e.event.messages
            for e in await agent.events(run.id)
            if isinstance(e.event, ContextBuildEvent)
        ]
        assert built and built[0] < 2 * len(run_ids), "the reloaded view is the compacted one"


async def test_the_summary_keeps_early_content_for_a_later_question(kind: str, pg_dsn: str) -> None:
    config = compacting(kind, pg_dsn)
    async with Server() as server:
        agent = await server.open_agent(config)
        await fill(agent)
        run = await agent.run("long", "What is the secret code word? Answer with the word only.")
        answer = await run.result()
        assert isinstance(answer, ResultEvent) and answer.subtype == "success"
        assert CODE in (answer.final_text or ""), answer.final_text
