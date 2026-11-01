"""Factory/setup terminal tails share the fenced status transaction."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_dispatch import make_agent


def job(rid, sid="s"):
    return SimpleNamespace(
        payload=json.dumps({"run_id": rid, "session_id": sid, "prompt": "hello"}).encode()
    )


@pytest.mark.parametrize("failure", ["factory", "session", "run_store", "session_store"])
async def test_setup_failure_commits_single_atomic_tail_and_terminal_replay(
    pool, monkeypatch, failure
):
    from linch import Agent, InMemoryRunStore
    from linch.sessions import InMemorySessionStore
    from linch.worker import Worker, enqueue_run

    rid = await enqueue_run(pool, "s", "hello")
    calls = 0

    async def factory(context):
        nonlocal calls
        calls += 1
        if failure == "factory":
            raise ValueError("invalid factory config")
        agent = make_agent(context)
        if failure == "run_store":
            agent.run_store = InMemoryRunStore()
        elif failure == "session_store":
            agent.session_store = InMemorySessionStore()
        return agent

    if failure == "session":

        async def broken(self, **kwargs):
            raise ValueError("session setup failed")

        monkeypatch.setattr(Agent, "session", broken)
    worker = Worker(pool, factory)
    await worker._handle(job(rid))
    rows = await worker._run_store.load_events(rid)
    assert [r.seq for r in rows] == [1, 2]
    assert [r.event.type for r in rows] == ["error", "result"]
    assert rows[0].event.error["name"] == "ValueError"
    assert rows[0].event.error["retryable"] is False
    assert rows[1].event.subtype == rows[1].event.stop_reason == "error"
    assert (await worker._run_store.load_run(rid)).status == "failed"
    await worker._handle(job(rid))
    assert calls == 1
    assert await worker._run_store.load_events(rid) == rows


async def test_failure_tail_database_error_leaves_run_retryable_and_queue_unacknowledged(pool):
    import asyncpg

    from linch.worker import Worker, enqueue_run

    rid = await enqueue_run(pool, "s", "hello")

    async def factory(context):
        raise ValueError("bad factory")

    worker = Worker(pool, factory)
    await pool.execute("""
        CREATE FUNCTION reject_terminal_event() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'event storage failed'; END $$;
        CREATE TRIGGER reject_terminal_event BEFORE INSERT ON run_events
        FOR EACH ROW EXECUTE FUNCTION reject_terminal_event();
    """)
    with pytest.raises(asyncpg.PostgresError, match="event storage failed"):
        await worker._handle(job(rid))
    record = await worker._run_store.load_run(rid)
    assert record.status == "pending"
    assert "errors" not in record.meta
    assert await worker._run_store.load_events(rid) == []
    assert await pool.fetchval("SELECT count(*) FROM pgqueuer") == 1
    await pool.execute("DROP TRIGGER reject_terminal_event ON run_events")
    await worker._handle(job(rid))
    assert (await worker._run_store.load_run(rid)).status == "failed"
    assert len(await worker._run_store.load_events(rid)) == 2


@pytest.mark.parametrize("unresolved", [False, True])
async def test_progressed_factory_failure_uses_receipts_instead_of_stale_checkpoint(
    pool, unresolved
):
    from linch.model_calls import ModelCallIntent, ModelCallReceipt, _ModelCallLedger
    from linch.run_store import RunCheckpoint
    from linch.types import Usage
    from linch.worker import Worker, enqueue_run

    rid = await enqueue_run(pool, "s", "hello")

    async def factory(context):
        raise ValueError("tenant configuration changed")

    worker = Worker(pool, factory)
    store = worker._run_store
    await store.create_run(
        "s", id=rid, initial_checkpoint=RunCheckpoint("provider_pending", "hello", 0, Usage())
    )

    async def persist(event):
        await store.append_event(rid, event)

    ledger = _ModelCallLedger(rid, persist=persist)
    intent = ModelCallIntent(
        logical_call_id="logical",
        delivery_attempt_id="delivery",
        parent_logical_call_id=None,
        kind="primary",
        run_id=rid,
        turn_index=0,
        requested_model="m",
        request_fingerprint="fingerprint",
        strategy_id=None,
    )
    await ledger.begin(intent)
    await ledger.mark_dispatch_started(intent)
    await ledger.settle(
        ModelCallReceipt(
            logical_call_id="logical",
            delivery_attempt_id="delivery",
            kind="primary",
            run_id=rid,
            turn_index=0,
            actual_model="m",
            usage_status="reported",
            usage=Usage(input_tokens=111, output_tokens=22),
            cost_usd=0.125,
            disposition="accepted",
            rejection_reason=None,
            accepted_text=None,
            accepted_text_sha256=None,
            duration_ms=17,
            source_context_fingerprint=None,
            request_fingerprint="fingerprint",
            strategy_id=None,
        )
    )
    if unresolved:
        second = replace(intent, logical_call_id="logical-2", delivery_attempt_id="delivery-2")
        await ledger.begin(second)
        await ledger.mark_dispatch_started(second)
    await worker._handle(job(rid))
    rows = await store.load_events(rid)
    result = rows[-1].event
    assert result.type == "result"
    assert result.total_usage == Usage(input_tokens=111, output_tokens=22)
    assert result.total_cost_usd == 0.125
    assert (await store.load_run(rid)).checkpoint.total_usage == result.total_usage
    assert result.usage_complete is (not unresolved)
    assert [r.event.type for r in rows][-2:] == ["error", "result"]
    assert [r.seq for r in rows] == list(range(1, len(rows) + 1))


async def test_setup_failure_result_preserves_persisted_trace_id(pool):
    from linch.worker import Worker, enqueue_run

    rid = await enqueue_run(pool, "s", "hello", meta={"trace_id": "trace-from-run"})
    observed = []

    async def factory(context):
        from linch._tracing import trace_id_for

        observed.append(trace_id_for(context.run.id))
        raise ValueError("invalid factory config")

    worker = Worker(pool, factory)
    queued = job(rid)
    payload = json.loads(queued.payload)
    payload["traceparent"] = f"00-{'a' * 32}-{'b' * 16}-01"
    queued.payload = json.dumps(payload).encode()
    await worker._handle(queued)
    result = (await worker._run_store.load_events(rid))[-1].event
    assert observed == ["a" * 32]
    assert result.trace_id == "trace-from-run"


@pytest.mark.parametrize("failure_kind", ["permission", "os", "data", "interface", "released"])
async def test_setup_permanent_store_errors_terminalize_the_run(pool, failure_kind):
    import asyncpg

    from linch.worker import Worker, enqueue_run

    failure = {
        "permission": PermissionError("certificate denied"),
        "os": OSError("permanent failure"),
        "data": asyncpg.DataError("invalid argument"),
        "interface": asyncpg.InterfaceError("another operation"),
        "released": asyncpg.InterfaceError(
            "cannot call Connection.fetch(): connection has been released back to the pool"
        ),
    }[failure_kind]
    rid = await enqueue_run(pool, "s", "hello")

    async def factory(context):
        raise failure

    worker = Worker(pool, factory)
    await worker._handle(job(rid))
    rows = await worker._run_store.load_events(rid)
    assert (await worker._run_store.load_run(rid)).status == "failed"
    assert [row.event.type for row in rows][-2:] == ["error", "result"]
