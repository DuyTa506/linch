"""Measure transactional run enqueue including PgQueuer's commit-time NOTIFY.

Run from the repo: PYTHONPATH=src python examples/worker/benchmark_enqueue.py
Requires AGENT_KIT_TEST_PG_DSN (or LINCH_POSTGRES_DSN). Uses an isolated schema.
"""

from __future__ import annotations

import asyncio
import json
import os
from statistics import median
from time import perf_counter
from uuid import uuid4

import asyncpg
from pgqueuer import Queries, QueueManager

from linch.worker import RunRequest, enqueue_runs, install


async def enqueuer(
    pool: asyncpg.Pool, chunks: list[list[RunRequest]], concurrency: int, index: int
) -> None:
    for chunk in chunks[index::concurrency]:
        await enqueue_runs(pool, chunk)


async def main() -> None:
    dsn = os.getenv("AGENT_KIT_TEST_PG_DSN") or os.environ["LINCH_POSTGRES_DSN"]
    admin = await asyncpg.connect(dsn)
    schema = f"worker_bench_{uuid4().hex}"
    await admin.execute(f"CREATE SCHEMA {schema}")
    try:
        channel = QueueManager(Queries.from_asyncpg_connection(admin)).channel
        await admin.add_listener(channel, lambda *_: None)
        pool = await asyncpg.create_pool(
            dsn, min_size=33, max_size=33, server_settings={"search_path": schema}
        )
        try:
            await install(pool)
            print(
                json.dumps(
                    {
                        "postgres": await pool.fetchval("SELECT version()"),
                        "runs_per_sample": 3072,
                        "samples": 3,
                        "batch_size": 32,
                        "notify_listener": True,
                    }
                ),
                flush=True,
            )
            for size in (1, 32):
                for concurrency in (1, 8, 32):
                    rates = []
                    for _ in range(3):
                        await pool.execute("TRUNCATE runs,pgqueuer")
                        chunks = [
                            [RunRequest(f"s{i}-{j}", "tick") for j in range(size)]
                            for i in range(3072 // size)
                        ]

                        started = perf_counter()
                        await asyncio.gather(
                            *(enqueuer(pool, chunks, concurrency, i) for i in range(concurrency))
                        )
                        rates.append(3072 / (perf_counter() - started))
                    print(
                        json.dumps(
                            {
                                "batch_size": size,
                                "enqueuers": concurrency,
                                "runs_per_s": round(median(rates), 1),
                                "samples": [round(v, 1) for v in rates],
                            }
                        ),
                        flush=True,
                    )
        finally:
            await pool.close()
    finally:
        await admin.execute(f"DROP SCHEMA {schema} CASCADE")
        await admin.close()


if __name__ == "__main__":
    asyncio.run(main())
