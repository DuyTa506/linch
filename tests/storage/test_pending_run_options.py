"""The public run option adopts queued IDs through either run-store backend."""

from __future__ import annotations

from uuid import uuid4

import pytest


@pytest.fixture(params=["memory", "postgres"])
def kind(request):
    return request.param


@pytest.fixture
async def pending_store(kind, pg_dsn):
    from linch import InMemoryRunStore, PostgresRunStore

    if kind == "memory":
        store = InMemoryRunStore()
    else:
        store = PostgresRunStore(pg_dsn)
        await store.__aenter__()
    try:
        yield store, pg_dsn
    finally:
        await store.close()


async def seed_pending(store, dsn, run_id, session_id="s"):
    from linch import InMemoryRunStore

    if isinstance(store, InMemoryRunStore):
        await store.create_run(session_id, id=run_id, meta={"queued": True})
        store._runs[run_id].status = "pending"  # Test fixture: only Postgres dispatches jobs.
    else:
        import asyncpg

        conn = await asyncpg.connect(dsn)
        try:
            async with conn.transaction():
                await store.create_pending_run(conn, session_id, id=run_id, meta={"queued": True})
        finally:
            await conn.close()


def make_agent(store):
    from linch import Agent, empty_tools
    from linch.ext.evals import ScriptedProvider, TextTurn
    from linch.sessions import InMemorySessionStore

    return Agent(
        model="m",
        provider=ScriptedProvider([TextTurn("done")]),
        tools=empty_tools(),
        session_store=InMemorySessionStore(),
        run_store=store,
    )


async def test_run_options_adopts_pending_run_id(pending_store):
    from linch import RunOptions

    store, dsn = pending_store
    rid = f"options-pending-{uuid4()}"
    await seed_pending(store, dsn, rid)
    agent = make_agent(store)
    try:
        session = await agent.session(id="s")
        events = [e async for e in session.run("hello", RunOptions(run_id=rid))]
        record = await store.load_run(rid)
        assert record.status == "completed"
        assert record.meta["queued"] is True
        assert "run_contract" in record.meta
        systems = [e for e in events if e.type == "system"]
        assert len(systems) == 1 and systems[0].run_id == rid
    finally:
        await agent.close()


@pytest.mark.parametrize("status", ["other-session", "running", "completed", "failed", "aborted"])
async def test_run_options_rejects_owned_or_nonpending_id(pending_store, status):
    from linch import RunOptions

    store, dsn = pending_store
    rid = f"options-existing-{uuid4()}"
    await seed_pending(store, dsn, rid, "other" if status == "other-session" else "s")
    if status != "other-session":
        await store.create_run("s", id=rid)
        if status == "completed":
            from linch import RunCheckpoint, Usage

            await store.mark_completed(rid, RunCheckpoint("started", "hello", 1, Usage()))
        elif status == "failed":
            await store.mark_failed(rid)
        elif status == "aborted":
            from linch import RunCheckpoint, Usage

            await store.save_checkpoint(
                rid, RunCheckpoint("aborted", "hello", 1, Usage()), status="aborted"
            )
    before = await store.load_run(rid)
    agent = make_agent(store)
    try:
        session = await agent.session(id="s")
        with pytest.raises(ValueError):
            async for _ in session.run("hello", RunOptions(run_id=rid)):
                pass
        assert await store.load_run(rid) == before
        assert await store.load_events(rid) == []
    finally:
        await agent.close()
