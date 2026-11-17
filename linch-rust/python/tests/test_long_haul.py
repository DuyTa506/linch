"""The long-haul harness: kill the server in the middle of a task, with a
compaction that fires on every turn, then finish the work and assert on what
is durable (ledger rows, files, the event journal), not on what the model said.

The model is `model_stub.ModelStub`, which answers from the request alone, so
the run only continues correctly if the ledger, the goal and the files reach
it from outside the conversation. Postgres, skipped without
`LINCH_TEST_PG_DSN`. See `docs/usage/long-running.md`.
"""

from __future__ import annotations

import asyncio
import os
import signal
import uuid
from collections.abc import Iterator
from typing import Any, cast

import pytest
from model_stub import MARKER, SUBJECTS, ModelStub

from linch_rust import (
    AgentConfig,
    CompactionEvent,
    ConnectionClosed,
    ResultEvent,
    RunLeased,
    Server,
    ToolCallStartEvent,
)

LIST = "report-ledger"
NAMESPACE = "longhaul"
GOAL = "The report is done when /report.md has one section per file in /in and no end marker."
FILES = {
    "/in/a.txt": "alpha notes ALPHA-7",
    "/in/b.txt": "beta notes BETA-9",
    "/in/c.txt": "gamma notes GAMMA-3",
}


@pytest.fixture
def pg_dsn() -> Iterator[str]:
    dsn = os.environ.get("LINCH_TEST_PG_DSN")
    if not dsn:
        pytest.skip("LINCH_TEST_PG_DSN is not set")
    import psycopg
    from psycopg import sql

    schema = f"haul_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            separator = "&" if "?" in dsn else "?"
            yield f"{dsn}{separator}options=-c%20search_path%3D{schema}"
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def haul(dsn: str, base_url: str) -> AgentConfig:
    """Postgres stores with the shortest lease, a ledger, the virtual
    filesystem, the goal as a system section, and compaction on every turn."""
    config: dict[str, Any] = {
        "namespace": NAMESPACE,
        "spec": {
            "model": "stub-model",
            "system": {"sections": [{"name": "goal", "text": GOAL, "order": 0}]},
            "compaction": {"threshold_tokens": 1, "keep_recent_turns": 1},
        },
        "provider": {"id": "vllm", "base_url": base_url, "context_window": 8000},
        "stores": {"type": "postgres", "dsn": dsn, "lease_ttl_ms": 1000},
        "permissions": {"mode": "skip_dangerous", "project_root": "/"},
        "filesystem": {},
        "tasks": True,
        "task_ledger": {"list": LIST},
    }
    return cast(AgentConfig, config)


def durable(dsn: str) -> dict[str, Any]:
    """The ledger rows and the report, read straight from the tables."""
    import psycopg

    with psycopg.connect(dsn) as conn:
        tasks = conn.execute(
            "SELECT subject, status FROM linch_tasks"
            " WHERE namespace = %s AND session_id = %s ORDER BY task_id",
            (NAMESPACE, LIST),
        ).fetchall()
        report = conn.execute(
            "SELECT content FROM linch_files WHERE namespace = %s AND path = '/report.md'",
            (NAMESPACE,),
        ).fetchone()
    return {"tasks": tasks, "report": report[0] if report else None}


def seed_files(dsn: str) -> None:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        for path, content in FILES.items():
            conn.execute(
                "INSERT INTO linch_files (namespace, path, content) VALUES (%s, %s, %s)",
                (NAMESPACE, path, content),
            )


def assert_done(dsn: str) -> None:
    state = durable(dsn)
    assert state["tasks"] == [(subject, "completed") for subject in SUBJECTS]
    report = state["report"]
    assert report is not None and MARKER not in report
    for file, token in [("a.txt", "ALPHA-7"), ("b.txt", "BETA-9"), ("c.txt", "GAMMA-3")]:
        assert f"## {file}\nKey token: {token}\n" in report, report


async def killed_midway(dsn: str, stub: ModelStub) -> str:
    """Start the report in session `init` and SIGKILL the server while a
    model call is in flight; the run id."""
    async with Server() as server:
        agent = await server.open_agent(haul(dsn, stub.base_url))
        seed_files(dsn)
        run = await agent.run("init", "Build the report. Follow the ledger.")
        await asyncio.wait_for(stub.held.wait(), 60)
        os.kill(server.pid, signal.SIGKILL)
        with pytest.raises(ConnectionClosed):
            await run.result()
    return run.id


async def tool_starts(agent: Any, run_id: str) -> list[str]:
    events = [e.event for e in await agent.events(run_id)]
    return [e.tool_name for e in events if isinstance(e, ToolCallStartEvent)]


async def test_a_killed_run_resumes_and_finishes_from_the_durable_state(pg_dsn: str) -> None:
    async with ModelStub(hold_at=9) as stub:
        run_id = await killed_midway(pg_dsn, stub)
        midway = durable(pg_dsn)
        assert midway["tasks"] and len(midway["tasks"]) == 4, "the ledger survived the kill"
        assert midway["tasks"][0][1] == "completed" and midway["tasks"][-1][1] == "pending"

        async with Server() as server:
            agent = await server.open_agent(haul(pg_dsn, stub.base_url))
            for _ in range(20):
                try:
                    result = await (await agent.resume(run_id)).result()
                    break
                except RunLeased as leased:
                    await asyncio.sleep(leased.expires_in or 0.2)
            else:
                raise AssertionError("the killed server's lease never ran out")
            assert isinstance(result, ResultEvent) and result.final_text == "Report complete."
            names = await tool_starts(agent, run_id)
            events = [e.event for e in await agent.events(run_id)]

    assert_done(pg_dsn)
    # Nothing ran twice across the kill: four tasks made, one skeleton, one
    # read and one edit per file plus the final edit, four tasks completed.
    count = {name: names.count(name) for name in set(names)}
    assert count == {
        "TaskCreate": 4,
        "write_file": 1,
        "read_file": 3,
        "edit_file": 4,
        "TaskUpdate": 4,
    }
    assert any(isinstance(e, CompactionEvent) for e in events), "compaction fired"
    assert stub.summaries >= 1


async def test_a_fresh_session_finishes_the_work_from_the_ledger_alone(pg_dsn: str) -> None:
    async with ModelStub(hold_at=9) as stub:
        run_id = await killed_midway(pg_dsn, stub)
        async with Server() as server:
            agent = await server.open_agent(haul(pg_dsn, stub.base_url))
            # No resume: a new session, whose conversation starts empty.
            run = await agent.run("fresh", "Continue the report. Follow the ledger.")
            result_run_id = run.id
            result = await run.result()
            assert isinstance(result, ResultEvent) and result.final_text == "Report complete."
            cut = await tool_starts(agent, run_id)
            fresh = await tool_starts(agent, result_run_id)

    assert_done(pg_dsn)
    assert cut.count("TaskCreate") == 4, "the cut run made the ledger"
    assert "TaskCreate" not in fresh, "the fresh session only worked it"
