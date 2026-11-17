"""Live: compaction of a long session on vLLM, and the session reloading
on a new server. Skips without the live environment.
"""

from __future__ import annotations

import pytest
from live_sched_support import live_config, pg_dsn, seqs_increasing, vllm

from linch_rust import CompactionEvent, ContextBuildEvent, ResultEvent, Server

__all__ = ["pg_dsn", "vllm"]

pytestmark = pytest.mark.live

CODE = "PELICAN-7421"


def filler(n: int) -> str:
    """About 500 tokens of distinct text, so the view grows each turn."""
    words = " ".join(f"log{n}-{i}:ok" for i in range(220))
    return f"Log excerpt {n}: {words}. Reply with the single word OK."


async def test_a_long_session_compacts_and_still_answers_about_early_content(
    vllm: str, pg_dsn: str
) -> None:
    config = live_config(
        vllm,
        "compact",
        pg_dsn,
        spec={
            "max_output_tokens": 300,
            "compaction": {"threshold_tokens": 2500, "keep_recent_turns": 2},
        },
    )
    run_ids: list[str] = []
    async with Server() as server:
        agent = await server.open_agent(config)
        run = await agent.run(
            "long",
            f"IMPORTANT: remember that the secret code word is {CODE}. "
            "Acknowledge with the single word OK.",
        )
        assert isinstance(await run.result(), ResultEvent)
        run_ids.append(run.id)
        for n in range(1, 8):
            run = await agent.run("long", filler(n))
            assert isinstance(await run.result(), ResultEvent)
            run_ids.append(run.id)

        compactions = []
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

        run = await agent.run("long", "What is the secret code word? Answer with the word only.")
        answer = await run.result()
        assert isinstance(answer, ResultEvent) and answer.subtype == "success"
        assert CODE in (answer.final_text or ""), answer.final_text

    # A new server loads the session from Postgres: the compacted view
    # persisted, and the early content is still answerable.
    async with Server() as server:
        agent = await server.open_agent(config)
        run = await agent.run(
            "long", "Again: what is the secret code word? Answer with the word only."
        )
        again = await run.result()
        assert isinstance(again, ResultEvent) and again.subtype == "success"
        assert CODE in (again.final_text or ""), again.final_text
