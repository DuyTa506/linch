"""Worker dispatch contracts against an isolated, real Postgres schema."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from tests.support.agents import make_worker_agent as make_agent


def test_core_import_does_not_load_worker_dependencies():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import linch, sys; assert 'worker' not in linch.__all__; "
            "assert not any(k.startswith('pgqueuer') for k in sys.modules)",
        ],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


async def wait_for(predicate, timeout=10):
    async with asyncio.timeout(timeout):
        while not (value := await predicate()):
            await asyncio.sleep(0.02)
        return value


@asynccontextmanager
async def running(*workers):
    tasks = [asyncio.create_task(worker.run()) for worker in workers]
    try:
        yield
    finally:
        for worker in workers:
            worker.stop()
        await asyncio.wait_for(asyncio.gather(*tasks), 10)


async def test_enqueue_is_atomic_pending_and_idempotent(pool):
    from linch.worker import enqueue_run

    rid = "one"
    await asyncio.gather(*(enqueue_run(pool, "s", "hello", run_id=rid) for _ in range(8)))
    assert await pool.fetchval("SELECT status FROM runs WHERE id=$1", rid) == "pending"
    assert await pool.fetchval("SELECT count(*) FROM pgqueuer") == 1
    row = await pool.fetchrow("SELECT dedupe_key,payload FROM pgqueuer")
    assert row["dedupe_key"] == "run:one"
    payload = json.loads(row["payload"])
    assert payload["run_id"] == "one" and payload["session_id"] == "s"
    with pytest.raises(ValueError, match="session"):
        await enqueue_run(pool, "other", "hello", run_id=rid)
    await pool.execute(
        "CREATE FUNCTION reject_job() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN RAISE EXCEPTION 'queue unavailable'; END $$; "
        "CREATE TRIGGER reject_job BEFORE INSERT ON pgqueuer "
        "FOR EACH ROW EXECUTE FUNCTION reject_job()"
    )
    with pytest.raises(Exception, match="queue unavailable"):
        await enqueue_run(pool, "free-session", "hello", run_id="rolled-back")
    assert await pool.fetchval("SELECT count(*) FROM runs WHERE id='rolled-back'") == 0


async def test_two_workers_obey_fleet_limits_and_lease_exclusion(pool):
    from pgqueuer import Queries

    from linch.ext.evals import ScriptedProvider, TextTurn
    from linch.worker import Worker, enqueue_run

    active = 0
    peak = 0
    starts = 0

    class SlowProvider(ScriptedProvider):
        async def stream(self, request, **kwargs):
            nonlocal active, peak, starts
            active += 1
            starts += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.2)
                async for event in super().stream(request, **kwargs):
                    yield event
            finally:
                active -= 1

    async def factory(job):
        return make_agent(job, SlowProvider([TextTurn("done")]))

    workers = [Worker(pool, factory, queues={"default": 1}, poll_interval_s=0.05) for _ in range(2)]
    for i in range(4):
        await enqueue_run(pool, f"s{i}", "hello")
    async with running(*workers):
        await wait_for(
            lambda: pool.fetchval("SELECT count(*)=4 FROM runs WHERE status='completed'")
        )
    assert peak == 1 and starts == 4
    # Two differently deduplicated jobs deliberately target one run; D1 still excludes them.
    rid = await enqueue_run(pool, "same-session", "hello")
    raw = await pool.fetchrow(
        "SELECT entrypoint,payload FROM pgqueuer WHERE dedupe_key=$1", f"run:{rid}"
    )
    await Queries.from_asyncpg_pool(pool).enqueue(raw["entrypoint"], bytes(raw["payload"]))
    workers = [Worker(pool, factory, queues={"default": 2}, poll_interval_s=0.05) for _ in range(2)]
    async with running(*workers):
        await wait_for(lambda: pool.fetchval("SELECT count(*)=0 FROM pgqueuer"))
    assert starts == 5, [dict(r) for r in await pool.fetch("SELECT id,status,meta FROM runs")]
    assert await pool.fetchval("SELECT status FROM runs WHERE id=$1", rid) == "completed"


async def test_infrastructure_claim_failure_is_retried(pool):
    from linch.worker import Worker, enqueue_run

    rid = await enqueue_run(pool, "s", "hello")
    await pool.execute(
        "CREATE FUNCTION reject_claim() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN IF NEW.lease_owner IS NOT NULL THEN "
        "RAISE EXCEPTION 'store unreachable'; END IF; RETURN NEW; END $$; "
        "CREATE TRIGGER reject_claim BEFORE UPDATE ON runs "
        "FOR EACH ROW EXECUTE FUNCTION reject_claim()"
    )

    async def factory(job):
        return make_agent(job)

    worker = Worker(pool, factory, retry_delay_s=0.1, poll_interval_s=0.02)
    async with running(worker):
        await wait_for(lambda: pool.fetchval("SELECT attempts>0 FROM pgqueuer"))
        assert await pool.fetchval("SELECT status FROM runs WHERE id=$1", rid) == "pending"
        await pool.execute("DROP TRIGGER reject_claim ON runs")
        await wait_for(
            lambda: pool.fetchval("SELECT status='completed' FROM runs WHERE id=$1", rid)
        )


async def test_worker_preserves_a_contract_persisted_before_first_checkpoint(pool):
    from linch.run_store import build_run_contract, run_meta_with_contract
    from linch.worker import Worker, enqueue_run

    contract = build_run_contract(primary_model="original")
    original = run_meta_with_contract(contract)
    rid = await enqueue_run(pool, "s", "hello", meta=original)

    async def factory(job):
        return make_agent(job)

    async with running(Worker(pool, factory, poll_interval_s=0.02)):
        await wait_for(lambda: pool.fetchval("SELECT count(*)=0 FROM pgqueuer"))
    row = await pool.fetchrow("SELECT status,meta FROM runs WHERE id=$1", rid)
    assert row["status"] == "failed"
    assert json.loads(row["meta"])["run_contract"] == original["run_contract"]


async def test_lease_loss_during_failure_recording_retries_nonterminal_run(pool, monkeypatch):
    from linch import PostgresRunStore
    from linch.worker import Worker, enqueue_run

    rid = await enqueue_run(pool, "s", "hello")
    calls = 0
    failed_leases = []
    real_mark_failed = PostgresRunStore.mark_failed

    async def mark_failed(store, run_id, checkpoint=None, error=None, *, events=(), lease=None):
        failed_leases.append(lease)
        return await real_mark_failed(store, run_id, checkpoint, error, events=events, lease=lease)

    monkeypatch.setattr(PostgresRunStore, "mark_failed", mark_failed)

    async def factory(job):
        nonlocal calls
        calls += 1
        if calls == 1:
            await pool.execute("UPDATE runs SET lease_epoch=lease_epoch+1 WHERE id=$1", rid)
            raise ValueError("factory failed after ownership changed")
        return make_agent(job)

    async with running(Worker(pool, factory, poll_interval_s=0.02, retry_delay_s=0.05)):
        await wait_for(
            lambda: pool.fetchval("SELECT status='completed' FROM runs WHERE id=$1", rid)
        )
        await wait_for(lambda: pool.fetchval("SELECT count(*)=0 FROM pgqueuer"))
    assert calls == 2
    assert len(failed_leases) == 1
    assert failed_leases[0] is not None
    assert failed_leases[0].run_id == rid
    assert (
        await pool.fetchval("SELECT lease_epoch FROM runs WHERE id=$1", rid)
        > failed_leases[0].epoch
    )
    assert not pool.is_closing()


async def test_failed_model_run_is_acknowledged_without_retry(pool):
    from linch.ext.evals import ScriptedProvider
    from linch.worker import Worker, enqueue_run

    calls = 0

    async def factory(job):
        nonlocal calls
        calls += 1
        return make_agent(job, ScriptedProvider([]))  # Provider fails inside the run.

    rid = await enqueue_run(pool, "s", "hello")
    async with running(Worker(pool, factory, poll_interval_s=0.02, retry_delay_s=0.05)):
        await wait_for(lambda: pool.fetchval("SELECT count(*)=0 FROM pgqueuer"))
    assert calls == 1
    assert await pool.fetchval("SELECT status FROM runs WHERE id=$1", rid) == "failed"
