"""Schedule occurrences share the dispatch transaction and are retry safe."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest
from test_dispatch import make_agent, running, wait_for


async def test_schedule_fire_advances_once_and_cancel_stops_future_runs(pool):
    from linch.worker import Worker, cancel_schedule, create_schedule, list_schedules

    definition = await create_schedule(pool, "s", "repeat", interval_s=0.2)
    assert [s.id for s in await list_schedules(pool, session_id="s")] == [definition.id]
    assert await list_schedules(pool, session_id="other") == []

    async def factory(job):
        return make_agent(job)

    async with running(Worker(pool, factory, poll_interval_s=0.02)):
        await wait_for(lambda: pool.fetchval("SELECT count(*)>0 FROM runs"))
        assert not await cancel_schedule(pool, definition.id, session_id="other")
        assert await cancel_schedule(pool, definition.id, session_id="s")
        count = await pool.fetchval("SELECT count(*) FROM runs")
        await asyncio.sleep(0.5)
        assert await pool.fetchval("SELECT count(*) FROM runs") == count
    rows = await pool.fetch("SELECT id,status FROM runs")
    assert all(r["status"] == "completed" for r in rows)
    assert all(r["id"].startswith(f"schedule:{definition.id}:") for r in rows)


async def test_schedule_fire_transaction_replay_and_rollback(pool):

    from linch.worker import create_schedule
    from linch.worker.schedules import _fire

    s = await create_schedule(pool, "s", "tick", interval_s=60)
    raw = json.loads(await pool.fetchval("SELECT payload FROM pgqueuer"))
    # A queue-insert failure must leave both the run receipt and the schedule unchanged.
    await pool.execute(
        "CREATE FUNCTION reject_run_job() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN IF NEW.entrypoint LIKE 'linch.run:%' THEN "
        "RAISE EXCEPTION 'queue down'; END IF; RETURN NEW; END $$; "
        "CREATE TRIGGER reject_run_job BEFORE INSERT ON pgqueuer "
        "FOR EACH ROW EXECUTE FUNCTION reject_run_job()"
    )
    with pytest.raises(Exception, match="queue down"):
        await _fire(pool, raw)
    assert await pool.fetchval("SELECT count(*) FROM runs") == 0
    assert await pool.fetchval("SELECT next_fire FROM linch_worker_schedules") == s.next_fire
    await pool.execute("DROP TRIGGER reject_run_job ON pgqueuer")
    await _fire(pool, raw)
    await _fire(pool, raw)
    assert await pool.fetchval("SELECT count(*) FROM runs") == 1
    assert (
        await pool.fetchval("SELECT count(*) FROM pgqueuer WHERE entrypoint='linch.schedule'") == 2
    )
    assert await pool.fetchval("SELECT next_fire FROM linch_worker_schedules") > s.next_fire


async def test_cron_and_tool_registration_are_scoped_and_reversible(pool):
    from linch import ToolContext, ToolRegistry
    from linch.worker import create_schedule, register_schedule_tools

    start = datetime(2026, 10, 31, 8, 0, tzinfo=UTC)
    s = await create_schedule(pool, "cron-session", "report", cron="0 9 * * *", after=start)
    assert s.next_fire == datetime(2026, 10, 31, 9, 0, tzinfo=UTC)
    with pytest.raises(ValueError):
        await create_schedule(pool, "s", "bad", cron="wrong")
    with pytest.raises(ValueError):
        await create_schedule(pool, "s", "bad", interval_s=0)
    registry = ToolRegistry()
    disposer = register_schedule_tools(registry, pool, session_id="tools-session")
    ctx = ToolContext(session_id="tools-session", cwd=".", run_id="r", session_store=None)
    create = registry.get("CreateSchedule")
    result = await create.execute({"payload": "later", "interval_s": 60}, ctx)
    assert not result.is_error
    result = await registry.get("ListSchedules").execute({}, ctx)
    assert "later" in result.content
    assert "report" not in result.content
    await disposer()
    await disposer()
    assert not registry.list()
