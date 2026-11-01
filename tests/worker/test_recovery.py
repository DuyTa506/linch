"""Kill a real process: task cancellation is a graceful PgQueuer acknowledgement."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest
from test_dispatch import make_agent, running, wait_for

_CRASH_WORKER = r"""
import asyncio, sys
import asyncpg
from linch import Agent, empty_tools
from linch.ext.evals import ScriptedProvider, TextTurn
from linch.worker import Worker

class Blocked(ScriptedProvider):
    async def stream(self, request):
        await asyncio.Event().wait()
        async for event in super().stream(request):
            yield event

async def main():
    pool = await asyncpg.create_pool(sys.argv[1], min_size=1, max_size=5,
                                     server_settings={'search_path': sys.argv[2]})
    async def factory(job):
        return Agent(model='m', provider=Blocked([TextTurn('never')]),
                     tools=empty_tools(), permissions={'mode':'skip-dangerous'},
                     session_store=job.session_store,
                     run_store=job.run_store)
    await Worker(pool, factory, worker_id='worker-a', lease_ttl_s=.6,
                 stall_timeout_s=1.2, poll_interval_s=.05).run()
asyncio.run(main())
"""


async def test_killed_worker_is_repicked_resumed_and_old_epoch_is_fenced(pool):
    from linch import ErrorEvent, LeaseLostError, PostgresRunStore
    from linch.worker import Worker, enqueue_run

    rid = await enqueue_run(pool, "crash-session", "hello")
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"))
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _CRASH_WORKER,
        os.environ["AGENT_KIT_TEST_PG_DSN"],
        await pool.fetchval("SELECT current_schema()"),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stale = PostgresRunStore("", pool=pool)
    try:
        await wait_for(
            lambda: pool.fetchval(
                "SELECT checkpoint::jsonb->>'phase'='provider_pending' FROM runs WHERE id=$1", rid
            )
        )
        original = await stale.claim(rid, "worker-a", 0.6)
        assert await pool.fetchval("SELECT status::text FROM pgqueuer") == "picked"
        proc.kill()  # No Python finally, lease release, or PgQueuer ACK can run.
        await proc.wait()
        assert proc.returncode < 0
        resumed = []

        async def factory(job):
            resumed.append(job.run.checkpoint.phase if job.run.checkpoint else None)
            return make_agent(job)

        b = Worker(
            pool,
            factory,
            worker_id="worker-b",
            lease_ttl_s=0.6,
            stall_timeout_s=1.2,
            poll_interval_s=0.05,
        )
        async with running(b):
            await wait_for(
                lambda: pool.fetchval("SELECT status='completed' FROM runs WHERE id=$1", rid)
            )
            await wait_for(lambda: pool.fetchval("SELECT count(*)=0 FROM pgqueuer"))
        assert resumed == ["provider_pending"]
        assert await pool.fetchval("SELECT count(*) FROM runs") == 1
        messages = await pool.fetchval(
            "SELECT count(*) FROM messages WHERE message::jsonb->>'role'='user'"
        )
        assert messages == 1
        assert await pool.fetchval(
            "SELECT lease_epoch>$2 FROM runs WHERE id=$1", rid, original.epoch
        )
        before = await pool.fetchval("SELECT count(*) FROM run_events")
        with pytest.raises(LeaseLostError):
            await stale.append_event(rid, ErrorEvent(error="late worker write"))
        with pytest.raises(LeaseLostError):
            await stale.mark_failed(rid, error={"type": "late"})
        assert await pool.fetchval("SELECT count(*) FROM run_events") == before
        assert await pool.fetchval("SELECT status FROM runs WHERE id=$1", rid) == "completed"
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        _, stderr = await proc.communicate()
        assert b"Traceback" not in stderr, stderr.decode()
