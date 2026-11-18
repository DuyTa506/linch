"""Print the column names of the worker and run tables after one fire (to write the SQL checks)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import h

from linch_rust.scripted import MODEL, text_turn


async def main() -> None:
    with h.schema() as dsn:
        allow = h.tenant(
            dsn,
            urls=["http://127.0.0.1:1/v1"],
            max_schedules=4,
            max_queued_jobs=8,
            min_schedule_interval_ms=1000,
        )
        d = Path(__import__("tempfile").mkdtemp(prefix="du-x-"))
        conf = {
            "socket": str(d / "s.sock"),
            "tenants": {"acme": {"tokens": [{"value": "tok-x"}], "allow": allow}},
            "worker": {"dsn": {"value": dsn}, "poll_interval_ms": 250},
        }
        srv = h.Srv(conf, directory=d)
        await srv.start()
        cfg = {
            "namespace": "acme",
            "spec": {"model": MODEL, "max_turns": 2},
            "provider": {"id": "scripted", "turns": [text_turn("ok")] * 30},
            "stores": {"type": "postgres", "dsn": dsn, "lease_ttl_ms": 2000},
        }
        async with srv.connect("tok-x") as server:
            agent = await server.open_agent(cfg)
            await agent.create_schedule("tick", interval_ms=1000, id="s1")
            await asyncio.sleep(5)
            print(await agent.schedules())
            print((await agent.jobs())[:2])
        for t in (
            "linch_worker_jobs",
            "linch_worker_history",
            "linch_worker_schedules",
            "linch_runs",
        ):
            cols = h.q(
                dsn,
                "SELECT column_name, data_type FROM information_schema.columns WHERE "
                "table_schema=current_schema() AND table_name=%s ORDER BY ordinal_position",
                t,
            )
            print(t, [c[0] for c in cols])
        print(
            h.q(dsn, "select count(*) from linch_runs"),
            h.q(dsn, "select count(*) from linch_worker_history"),
        )
        await srv.kill()


asyncio.run(main())
