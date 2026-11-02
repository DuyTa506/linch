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


async def test_worker_runs_assigned_id_and_propagates_trace(pool):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from linch import Context
    from linch.worker import Worker, enqueue_run

    exporter = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(exporter))

    async def factory(job):
        ctx = Context()
        ctx.register("tracer_provider", tp)
        return make_agent(job, context=ctx)

    with tp.get_tracer("caller").start_as_current_span("caller") as caller:
        trace_id = caller.get_span_context().trace_id
        rid = await enqueue_run(pool, "s", "hello")
    payload = json.loads(await pool.fetchval("SELECT payload FROM pgqueuer"))
    assert payload["traceparent"].split("-")[1] == f"{trace_id:032x}"
    worker = Worker(pool, factory, lease_ttl_s=0.6, stall_timeout_s=1.2, poll_interval_s=0.05)
    async with running(worker):
        await wait_for(
            lambda: pool.fetchval("SELECT status='completed' FROM runs WHERE id=$1", rid)
        )
        await wait_for(lambda: pool.fetchval("SELECT count(*)=0 FROM pgqueuer"))
    assert await pool.fetchval("SELECT count(*) FROM runs") == 1
    spans = exporter.get_finished_spans()
    invoked = [s for s in spans if s.name.startswith("invoke_agent")]
    assert len(invoked) == 1
    assert invoked[0].context.trace_id == trace_id
    assert invoked[0].parent.span_id == caller.get_span_context().span_id
    await enqueue_run(pool, "s", "hello", run_id=rid)
    assert await pool.fetchval("SELECT count(*) FROM pgqueuer") == 0
    assert not pool.is_closing()
    tp.shutdown()


async def test_terminal_job_is_acked_without_building_agent(pool):
    from linch.worker import Worker, enqueue_run

    rid = await enqueue_run(pool, "s", "hello")
    await pool.execute("UPDATE runs SET status='failed' WHERE id=$1", rid)

    async def factory(job):
        pytest.fail("terminal run built an agent")

    async with running(Worker(pool, factory, poll_interval_s=0.05)):
        await wait_for(lambda: pool.fetchval("SELECT count(*)=0 FROM pgqueuer"))


async def test_failed_run_is_result_not_queue_retry(pool):
    from linch.worker import Worker, enqueue_run

    rid = await enqueue_run(pool, "s", "hello")
    calls = 0

    async def factory(job):
        nonlocal calls
        calls += 1
        raise ValueError("invalid tenant configuration")

    async with running(Worker(pool, factory, poll_interval_s=0.05)):
        await wait_for(lambda: pool.fetchval("SELECT count(*)=0 FROM pgqueuer"))
    assert calls == 1
    assert await pool.fetchval("SELECT status FROM runs WHERE id=$1", rid) == "failed"
    from linch import PostgresRunStore

    rows = await PostgresRunStore("", pool=pool).load_events(rid)
    assert [row.seq for row in rows] == [1, 2]
    assert [row.event.type for row in rows] == ["error", "result"]
    assert rows[-1].event.subtype == "error"
    assert rows[-1].event.stop_reason == "error"


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


async def test_lease_heartbeat_renews_then_cancels_on_loss(pool):
    from linch import PostgresRunStore
    from linch.worker import Worker, enqueue_run

    async def factory(job):
        return make_agent(job)

    worker = Worker(pool, factory, lease_ttl_s=0.3)
    rid = await enqueue_run(pool, "s", "hello")
    store = PostgresRunStore("", pool=pool)
    lease = await store.claim(rid, "owner", 0.3)

    async def blocked():
        await asyncio.Event().wait()

    run_task = asyncio.create_task(blocked())
    task = asyncio.create_task(worker._heartbeat(store, lease, run_task))
    try:
        await asyncio.sleep(0.45)
        assert not run_task.done()
        assert await pool.fetchval(
            "SELECT lease_expires_at>$2 FROM runs WHERE id=$1", rid, lease.expires_at
        )
        await pool.execute("UPDATE runs SET lease_epoch=lease_epoch+1 WHERE id=$1", rid)
        await asyncio.wait_for(task, 1)
        with pytest.raises(asyncio.CancelledError):
            await run_task
        assert await pool.fetchval("SELECT status FROM runs WHERE id=$1", rid) == "pending"
    finally:
        task.cancel()
        run_task.cancel()
        await asyncio.gather(task, run_task, return_exceptions=True)


async def test_named_queues_have_independent_fleet_limits(pool):
    from linch.ext.evals import ScriptedProvider, TextTurn
    from linch.worker import Worker, enqueue_run

    entered = asyncio.Event()
    release = asyncio.Event()

    class Blocked(ScriptedProvider):
        async def stream(self, request):
            entered.set()
            await release.wait()
            async for e in super().stream(request):
                yield e

    async def factory(job):
        provider = Blocked([TextTurn("blocked")]) if job.queue == "slow" else None
        return make_agent(job, provider)

    slow = await enqueue_run(pool, "s-slow", "hello", queue="slow")
    fast = await enqueue_run(pool, "s-fast", "hello", queue="fast")
    worker = Worker(pool, factory, queues={"slow": 1, "fast": 1}, poll_interval_s=0.02)
    async with running(worker):
        try:
            await asyncio.wait_for(entered.wait(), 3)
            await wait_for(
                lambda: pool.fetchval("SELECT status='completed' FROM runs WHERE id=$1", fast)
            )
            assert await pool.fetchval("SELECT status FROM runs WHERE id=$1", slow) == "running"
        finally:
            release.set()


async def test_worker_hands_factory_plain_store_and_releases_after_run(pool, monkeypatch):
    from linch import PostgresRunStore, Session
    from linch.worker import Worker, enqueue_run

    bound = []
    options = []
    real_run = Session.run

    def capture_run(session, prompt, run_options=None):
        options.append(run_options)
        return real_run(session, prompt, run_options)

    monkeypatch.setattr(Session, "run", capture_run)

    async def factory(job):
        bound.append(job.run_store)
        assert (
            await pool.fetchval("SELECT lease_owner FROM runs WHERE id=$1", job.run.id)
            == worker.worker_id
        )
        return make_agent(job)

    rid = await enqueue_run(pool, "s", "hello")
    worker = Worker(pool, factory, poll_interval_s=0.02)
    async with running(worker):
        await wait_for(lambda: pool.fetchval("SELECT count(*)=0 FROM pgqueuer"))
    assert len(bound) == 1 and type(bound[0]) is PostgresRunStore
    assert len(options) == 1 and options[0].lease.run_id == rid
    assert options[0].lease.owner == worker.worker_id
    assert await pool.fetchval("SELECT lease_owner FROM runs WHERE id=$1", rid) is None
    assert await pool.fetchval("SELECT status FROM runs WHERE id=$1", rid) == "completed"
    assert not pool.is_closing()


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


async def test_overlapping_batches_create_one_receipt_and_job_per_run(pool):
    from linch.worker import RunRequest, enqueue_runs

    requests = [RunRequest(f"s{i}", "hello", run_id=f"r{i}") for i in range(8)]
    ids = await asyncio.gather(enqueue_runs(pool, requests), enqueue_runs(pool, requests[::-1]))
    assert ids == [[f"r{i}" for i in range(8)], [f"r{i}" for i in reversed(range(8))]]
    assert await pool.fetchval("SELECT count(*) FROM runs") == 8
    assert await pool.fetchval("SELECT count(*) FROM pgqueuer") == 8
    with pytest.raises(ValueError, match="session"):
        await enqueue_runs(
            pool,
            [
                RunRequest("s-new", "new", run_id="new"),
                RunRequest("other-session", "bad", run_id="r7"),
            ],
        )
    assert await pool.fetchval("SELECT count(*) FROM runs WHERE id='new'") == 0


async def test_enqueue_session_conflict_rolls_back_entire_batch(pool):
    from linch import SessionBusyError
    from linch.worker import RunRequest, enqueue_run, enqueue_runs

    await enqueue_run(pool, "busy", "first", run_id="busy-owner", meta={"original": True})
    with pytest.raises(SessionBusyError) as rejected:
        await enqueue_runs(
            pool,
            [
                RunRequest("a-free", "first in batch", run_id="a-inserted-then-rolled-back"),
                RunRequest("busy", "conflict", run_id="z-rejected"),
            ],
        )
    assert rejected.value.run_id == "busy-owner"
    assert await pool.fetchval("SELECT count(*) FROM runs") == 1
    assert await pool.fetchval("SELECT count(*) FROM pgqueuer") == 1
    # A conflict between two brand-new batch members also rolls both back.
    with pytest.raises(SessionBusyError):
        await enqueue_runs(
            pool,
            [
                RunRequest("free", "one", run_id="new-a"),
                RunRequest("free", "two", run_id="new-b"),
            ],
        )
    assert await pool.fetchval("SELECT count(*) FROM runs") == 1
    assert await pool.fetchval("SELECT count(*) FROM pgqueuer") == 1


async def test_resubmitting_run_id_preserves_prompt_metadata_even_when_session_busy(pool):
    from linch.worker import enqueue_run

    await enqueue_run(pool, "s", "first", run_id="original", meta={"source": "first"})
    await pool.execute("UPDATE runs SET status='completed' WHERE id='original'")
    await enqueue_run(pool, "s", "next turn", run_id="next")
    assert (
        await enqueue_run(pool, "s", "changed", run_id="original", meta={"source": "new"})
        == "original"
    )
    assert json.loads(await pool.fetchval("SELECT meta FROM runs WHERE id='original'")) == {
        "source": "first"
    }
    payload = json.loads(
        await pool.fetchval("SELECT payload FROM pgqueuer WHERE dedupe_key='run:original'")
    )
    assert payload["prompt"] == "first"
    assert await pool.fetchval("SELECT count(*) FROM runs") == 2
    assert await pool.fetchval("SELECT count(*) FROM pgqueuer") == 2


async def test_enqueue_acquires_sessions_in_consistent_order_before_run_ids(monkeypatch):
    from types import SimpleNamespace

    pytest.importorskip("pgqueuer")
    from linch.worker import RunRequest
    from linch.worker.dispatch import _enqueue

    acquired = []

    class Connection:
        def is_in_transaction(self):
            return True

        @asynccontextmanager
        async def transaction(self):
            yield

        async def fetchrow(self, sql, rid, sid, ts, meta):
            acquired.append((sid, rid))
            return {
                "id": rid,
                "session_id": sid,
                "status": "pending",
                "checkpoint": None,
                "meta": meta,
                "created_at": ts,
                "updated_at": ts,
            }

    async def insert_jobs(*args, **kwargs):
        pass

    monkeypatch.setattr(
        "linch.worker.dispatch.Queries.from_asyncpg_connection",
        lambda conn: SimpleNamespace(enqueue=insert_jobs),
    )
    requests = [RunRequest("b", "b", run_id="a"), RunRequest("a", "a", run_id="z")]
    assert await _enqueue(Connection(), requests, None) == ["a", "z"]
    assert acquired == [("a", "z"), ("b", "a")]
