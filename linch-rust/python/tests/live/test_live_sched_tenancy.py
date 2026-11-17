"""Live: two tenants on one server and database cannot see each other's
runs, schedules or jobs; several runs at once on a paid provider; the
Postgres file backend keeps an agent's memory across sessions and servers.
Skips without the live environment.
"""

from __future__ import annotations

import asyncio

import pytest
from live_sched_support import (
    TOKEN,
    TOKEN_BETA,
    kind,
    live_config,
    needs_deepseek,
    pg_dsn,
    query,
    serving,
    tenant_allow,
    vllm,
)

from linch_rust import (
    JobNotFound,
    NotAllowed,
    ResultEvent,
    RunNotFound,
    Server,
    ToolCallEndEvent,
)

__all__ = ["kind", "pg_dsn", "vllm"]

pytestmark = pytest.mark.live

PONG = "Reply with the single word PONG and nothing else."


async def test_two_tenants_do_not_see_each_others_runs_schedules_or_jobs(
    vllm: str, pg_dsn: str
) -> None:
    tenants = {
        "acme": {"tokens": [{"value": TOKEN}], "allow": tenant_allow(pg_dsn)},
        "beta": {"tokens": [{"value": TOKEN_BETA}], "allow": tenant_allow(pg_dsn)},
    }
    async with serving(pg_dsn, tenants=tenants) as live:
        async with live.connect(TOKEN) as one, live.connect(TOKEN_BETA) as two:
            assert (one.tenant, two.tenant) == ("acme", "beta")
            acme = await one.open_agent(live_config(vllm, "acme", pg_dsn))
            beta = await two.open_agent(live_config(vllm, "beta", pg_dsn))

            run = await acme.run("s1", PONG)
            assert isinstance(await run.result(), ResultEvent)
            schedule = await acme.create_schedule(PONG, interval_ms=60_000, id="mine")
            job = await acme.enqueue("s2", PONG)
            await acme.wait_job(job["job_id"], timeout=60)

            with pytest.raises(RunNotFound):
                await beta.events(run.id)
            with pytest.raises(RunNotFound):
                await (await beta.resume(run.id)).result()
            assert await beta.schedules() == []
            assert not await beta.delete_schedule(schedule["id"]), "not beta's to delete"
            with pytest.raises(JobNotFound):
                await beta.job(job["job_id"])
            assert await beta.jobs() == []
            # Another tenant's namespace is refused at open.
            with pytest.raises(NotAllowed) as refused:
                await two.open_agent(live_config(vllm, "acme", pg_dsn))
            assert refused.value.kind == "namespace"
            # And another tenant's principal.
            with pytest.raises(NotAllowed) as impostor:
                await (
                    await beta.run("s9", PONG, principal={"tenant": "acme", "user": "eve"})
                ).result()
            assert impostor.value.kind == "principal"

            assert [s["id"] for s in await acme.schedules()] == ["mine"]
            assert await acme.delete_schedule("mine")


@needs_deepseek
async def test_eight_concurrent_runs_on_deepseek_all_finish(pg_dsn: str) -> None:
    config = live_config("deepseek", "scale", pg_dsn, spec={"max_output_tokens": 50})
    async with Server() as server:
        agent = await server.open_agent(config)

        async def one(n: int) -> ResultEvent:
            run = await agent.run(f"c{n}", f"Reply with the single word PONG{n}.")
            result = await run.result()
            assert isinstance(result, ResultEvent)
            return result

        results = await asyncio.gather(*(one(n) for n in range(8)))
        assert [r.subtype for r in results] == ["success"] * 8
        assert all(r.usage_complete for r in results)
        rows = query(
            pg_dsn,
            "SELECT count(*) FROM linch_runs WHERE namespace = 'scale'",
        )
        assert rows == [(8,)], "eight durable runs"


async def test_agent_memory_in_postgres_files_persists_across_sessions_and_servers(
    kind: str, pg_dsn: str
) -> None:
    config = live_config(
        kind,
        "memory",
        pg_dsn,
        filesystem={},
        permissions={"mode": "skip_dangerous", "project_root": "/"},
    )
    async with Server() as server:
        agent = await server.open_agent(config)
        run = await agent.run(
            "first",
            "Call the tool write_file with path '/notes/heron.txt' and content 'blue heron'. "
            "Then answer: saved.",
        )
        assert isinstance(await run.result(), ResultEvent)
        ends = [
            e.event for e in await agent.events(run.id) if isinstance(e.event, ToolCallEndEvent)
        ]
        assert [(e.tool_name, e.is_error) for e in ends] == [("write_file", False)], ends

    rows = query(pg_dsn, "SELECT count(*) FROM linch_files WHERE namespace = 'memory'")
    assert rows[0][0] >= 1, "the file is a row in Postgres"

    # A new server and a new session of the same agent read it back.
    async with Server() as server:
        agent = await server.open_agent(config)
        run = await agent.run(
            "second",
            "Call the tool read_file with path '/notes/heron.txt', then say its content.",
        )
        result = await run.result()
        assert isinstance(result, ResultEvent) and result.subtype == "success"
        ends = [
            e.event for e in await agent.events(run.id) if isinstance(e.event, ToolCallEndEvent)
        ]
        assert [(e.tool_name, e.is_error) for e in ends] == [("read_file", False)], ends
        assert "blue heron" in str(ends[0].result)
        assert "blue heron" in (result.final_text or "")
