"""Runnable fleet example with no model API key (uses a scripted provider).

pip install -e '.[worker]'
export LINCH_POSTGRES_DSN='postgresql://postgres:postgres@localhost/linch'
PYTHONPATH=src python examples/worker/minimal.py enqueue hello
PYTHONPATH=src python examples/worker/minimal.py worker  # run N copies
PYTHONPATH=src python examples/worker/minimal.py events RUN_ID
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys

import asyncpg

from linch import Agent, PostgresRunStore, empty_tools
from linch.evals import ScriptedProvider, TextTurn
from linch.sessions.postgres import PostgresSessionStore
from linch.worker import Worker, WorkerJob, enqueue_run, install


async def agent_factory(job: WorkerJob) -> Agent:
    # Resolve tenant/profile/provider credentials from job.run.meta here.
    return Agent(
        model="demo",
        provider=ScriptedProvider([TextTurn("Hello from a worker.")]),
        tools=empty_tools(),
        run_store=job.run_store,
        session_store=PostgresSessionStore("", pool=job.pool),
        permissions={"mode": "skip-dangerous"},
    )


async def main() -> None:
    async with asyncpg.create_pool(
        os.environ["LINCH_POSTGRES_DSN"], min_size=2, max_size=10
    ) as pool:
        await install(pool)
        mode = sys.argv[1] if len(sys.argv) > 1 else "worker"
        if mode == "enqueue":
            print(await enqueue_run(pool, "example-session", " ".join(sys.argv[2:]) or "hello"))
        elif mode == "worker":
            worker = Worker(pool, agent_factory, queues={"default": 4})
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, worker.stop)
            await worker.run()  # stop() drains active work before returning.
        elif mode == "events":
            store = PostgresRunStore("", pool=pool)
            seq = 0  # A reconnect supplies its last delivered sequence here.
            while True:
                for stored in await store.load_events(sys.argv[2], after_seq=seq):
                    print(stored.seq, stored.event, flush=True)
                    seq = stored.seq
                    if stored.event.type == "result":
                        return
                await asyncio.sleep(0.2)
        else:
            raise SystemExit("usage: minimal.py [worker|enqueue PROMPT|events RUN_ID]")


if __name__ == "__main__":
    asyncio.run(main())
