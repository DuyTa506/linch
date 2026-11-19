"""Live e2e (area long): the long-running harness pattern (L1-L4 of `expectations/long.md`).

A goal-contract system section, `tasks` with a shared `task_ledger`, a low
compaction threshold, Postgres stores, vLLM through the body-logging tee. The
model keeps the ledger over 6+ turns; the ledger rows and the request tails are
checked after the compaction and in a fresh session.
"""

from __future__ import annotations

from typing import Any

import pytest
from e2e_long_support import Tee, dump, msg_text
from live_core_support import LIVE_KIND, ONLY_VLLM, Live, live_server, make_config, needs_vllm
from live_sched_support import pg_dsn, query

from linch_rust import (
    CompactionEvent,
    ResultEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    tool,
)

__all__ = ["pg_dsn"]

pytestmark = [pytest.mark.live, needs_vllm]

LIVE: Live = ONLY_VLLM[0].values[0]  # type: ignore[assignment]
GOAL = (
    "GOAL-CONTRACT: the work is done when Step A, Step B and Step C are completed "
    "and Step D is still pending."
)
LIST = "e2e-ledger"


@tool(scope="read")
async def lookup_big(key: str) -> str:
    """Look up a bulky record by key."""
    return f"Record {key}: " + " ".join(f"{key}-field{i}=value{i}" for i in range(150))


PROMPT = (
    "Do this now with tool calls, without asking questions. First call TaskCreate four times, with "
    "the subjects exactly "
    "'Step A', 'Step B', 'Step C' and 'Step D' (a short description each). Then for Step A, "
    "Step B and Step C, in that order: call TaskUpdate to set the task in_progress, call "
    "lookup_big with the key a, b or c, then call TaskUpdate to set the task completed. "
    "Leave Step D pending: do not start it. Finish with one short sentence."
)


async def test_the_ledger_is_kept_across_6_turns_a_compaction_and_a_fresh_session(
    pg_dsn: str,
) -> None:
    tee = Tee("harness_ledger")
    out: dict[str, Any] = {}
    config = make_config(
        LIVE,
        LIVE.spec(
            max_turns=30,
            max_output_tokens=300,
            system={"sections": [{"name": "goal", "text": GOAL, "order": 0}]},
            compaction={"threshold_tokens": 1800, "keep_recent_turns": 2, "max_output_tokens": 500},
        ),
        stores={"type": "postgres", "dsn": pg_dsn},
        permissions={"mode": "skip_dangerous", "project_root": "/"},
        tasks=True,
        task_ledger={"list": LIST},
    )
    config["provider"] = {"id": LIVE_KIND, "base_url": tee.url}
    namespace = config["namespace"]
    try:
        async with live_server() as server:
            agent = await server.open_agent(config, tools=[lookup_big])
            run = await agent.run("init", PROMPT)
            result = await run.result()
            assert isinstance(result, ResultEvent), result
            events = [e.event for e in await agent.events(run.id)]
            first_run_requests = len(tee.chat())
            out["calls"] = [e.tool_name for e in events if isinstance(e, ToolCallStartEvent)]
            out["task_results"] = [
                "".join(p.get("text", "") for p in e.result.get("content", []))
                for e in events
                if isinstance(e, ToolCallEndEvent) and e.tool_name.startswith("Task")
            ][:12]
            out["assistant_turns"] = first_run_requests
            out["compactions"] = [
                (c.strategy, c.tokens_before, c.tokens_after)
                for c in events
                if isinstance(c, CompactionEvent)
            ]
            out["first_run_final"] = result.final_text
            fresh = await agent.run(
                "fresh", "Continue the work. The ledger says where it stands. Which step is open?"
            )
            fresh_result = await fresh.result()
            assert isinstance(fresh_result, ResultEvent)
            out["fresh_final"] = fresh_result.final_text
    finally:
        tee.close()
    rows = query(
        pg_dsn,
        "SELECT session_id, task_id, subject, status FROM linch_tasks WHERE namespace = %s "
        "ORDER BY session_id, task_id",
        namespace,
    )
    out["task_rows"] = [list(r) for r in rows]
    chats = tee.chat()
    out["requests"] = [
        {
            "i": i,
            "summary_call": msg_text(r["body"]["messages"][-1]).startswith(
                "The conversation to summarize ends above."
            ),
            "messages": len(r["body"]["messages"]),
            "goal_in_system": GOAL in msg_text(r["body"]["messages"][0]),
            "ledger_in_tail": "Task ledger:" in msg_text(r["body"]["messages"][-1]),
            "step_d_open_in_tail": "[pending] Step D" in msg_text(r["body"]["messages"][-1]),
            "prompt_tokens": r.get("prompt_tokens"),
        }
        for i, r in enumerate(chats)
    ]
    out["sample_tail"] = msg_text(chats[-1]["body"]["messages"][-1])[-700:]
    dump("harness_ledger", out)

    # L1: the goal section is in the system message of every request, compactions included.
    assert all(r["goal_in_system"] for r in out["requests"]), out["requests"]
    # L2: 6+ turns, the ledger rows hold the expected statuses, in the one shared list.
    assert out["assistant_turns"] >= 6, out["assistant_turns"]
    statuses = {r[2]: r[3] for r in out["task_rows"]}
    assert statuses == {
        "Step A": "completed",
        "Step B": "completed",
        "Step C": "completed",
        "Step D": "pending",
    }, out["task_rows"]
    assert {r[0] for r in out["task_rows"]} == {LIST}, "one ledger for every session"
    # L3: a compaction happened and the ledger block is still in the requests after it.
    assert out["compactions"], "the threshold was crossed"
    primaries = [r for r in out["requests"] if not r["summary_call"]]
    assert all(r["ledger_in_tail"] for r in primaries), out["requests"]
    # The summary call carries no request-only context (PR #156): no ledger block in it.
    assert not any(r["ledger_in_tail"] for r in out["requests"] if r["summary_call"])
    # L4: the fresh session's first request lists the open task.
    fresh_first = out["requests"][first_run_requests]
    assert fresh_first["messages"] <= 3 and fresh_first["step_d_open_in_tail"], fresh_first
    print(f"fresh session answer: {out['fresh_final']!r}")
