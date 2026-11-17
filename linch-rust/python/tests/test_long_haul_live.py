"""The long-haul scenario against a real model on a local vLLM server.

Skips without `LINCH_LIVE_VLLM_URL` (the server's `/v1` root) and
`LINCH_TEST_PG_DSN`; `LINCH_LIVE_VLLM_MODEL` names the model (the first one the
server lists when absent). One request at a time, small outputs, thinking off.

The server is killed in the middle of the first session, as in
`test_long_haul.py`, and later sessions start with an empty conversation; each
is told only to continue. The assertions are on the durable state: the ledger
rows, `/report.md`, and the journal of the cut run.
"""

from __future__ import annotations

import asyncio
import os
import signal
import urllib.request
from typing import Any, cast

import pytest
from test_long_haul import GOAL, LIST, NAMESPACE, durable, pg_dsn, seed_files  # noqa: F401

from linch_rust import (
    AgentConfig,
    ConnectionClosed,
    Server,
    ToolCallStartEvent,
)

URL = os.environ.get("LINCH_LIVE_VLLM_URL")
pytestmark = pytest.mark.skipif(not URL, reason="LINCH_LIVE_VLLM_URL is not set")

PROMPT = (
    "Build /report.md from the three text files in /in (a.txt, b.txt, c.txt): one "
    "section per file, headed `## <file name>`, with one line quoting the file's "
    "capitalised key token (for example ALPHA-7). Keep a task per step in the task "
    "ledger and mark each completed when it is done. Write the report to "
    "/report.md as you go, and read the ledger before anything else."
)
CONTINUE = "Continue the work. The ledger and /report.md say where it stands."


def model_name() -> str:
    named = os.environ.get("LINCH_LIVE_VLLM_MODEL")
    if named:
        return named
    import json

    with urllib.request.urlopen(f"{URL}/models", timeout=10) as reply:
        return str(json.load(reply)["data"][0]["id"])


def live(dsn: str) -> AgentConfig:
    config: dict[str, Any] = {
        "namespace": NAMESPACE,
        "spec": {
            "model": model_name(),
            "system": {"sections": [{"name": "goal", "text": GOAL, "order": 0}]},
            "compaction": {"threshold_tokens": 1500, "keep_recent_turns": 2},
            "max_turns": 14,
            "max_output_tokens": 1024,
            "thinking": {"type": "disabled"},
            "max_tool_concurrency": 1,
        },
        "provider": {"id": "vllm", "base_url": URL},
        "stores": {"type": "postgres", "dsn": dsn, "lease_ttl_ms": 1000},
        "permissions": {"mode": "skip_dangerous", "project_root": "/"},
        "filesystem": {},
        "tasks": True,
        "task_ledger": {"list": LIST},
    }
    return cast(AgentConfig, config)


def finished(dsn: str) -> bool:
    state = durable(dsn)
    report = state["report"] or ""
    tokens = ["ALPHA-7", "BETA-9", "GAMMA-3"]
    return (
        bool(state["tasks"])
        and all(status == "completed" for _, status in state["tasks"])
        and all(token in report for token in tokens)
    )


async def test_a_real_model_finishes_the_report_across_a_kill_and_fresh_sessions(
    pg_dsn: str,  # noqa: F811
) -> None:
    run_ids: list[str] = []
    async with Server() as server:
        agent = await server.open_agent(live(pg_dsn))
        seed_files(pg_dsn)
        run = await agent.run("init", PROMPT)
        run_ids.append(run.id)
        # Let the first session get a few tool calls in, then kill the server.
        for _ in range(240):
            await asyncio.sleep(0.5)
            if len(await agent.events(run.id)) >= 12:
                break
        os.kill(server.pid, signal.SIGKILL)
        with pytest.raises(ConnectionClosed):
            await run.result()

    async with Server() as server:
        agent = await server.open_agent(live(pg_dsn))
        for n in range(8):
            if finished(pg_dsn):
                break
            run = await agent.run(f"fresh-{n}", CONTINUE)
            run_ids.append(run.id)
            await run.result()
        starts: list[str] = []
        for run_id in run_ids:
            events = [e.event for e in await agent.events(run_id)]
            starts += [e.tool_name for e in events if isinstance(e, ToolCallStartEvent)]

    state = durable(pg_dsn)
    print(f"sessions: {len(run_ids)}, tool calls: {len(starts)}")
    print(f"tasks: {state['tasks']}")
    print(f"report:\n{state['report']}")
    assert finished(pg_dsn), (state, starts)
