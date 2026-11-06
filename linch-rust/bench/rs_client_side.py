"""Bench side "rs-client": the `linch_rust` client over the `linch-server` sidecar.

Run as `python -I rs_client_side.py JOB.json`; imports the client from
`linch-rust/python/src` and spawns the server binary the job names. CPU
counts this process plus the sidecar's threads; peak RSS is the sum of the
two processes' peaks.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "python" / "src"))

from common import Clock, Job, Result, load_turns, main, ok, peak_rss_mb, proc_hwm_mb  # noqa: E402
from linch_rust import ResultEvent, Server, tool  # noqa: E402


def echo_tool(spec: dict):
    @tool(
        scope="read",
        name=spec["name"],
        description=spec["description"],
        input_schema=spec["input_schema"],
    )
    async def echo(value: str) -> str:
        return value

    return echo


def config(job: Job, provider: dict, **extra) -> dict:
    spec = {"model": job["model"], "system_prompt": job["system"]}
    if job.get("max_tool_concurrency") is not None:
        spec["max_tool_concurrency"] = job["max_tool_concurrency"]
    out = {
        "namespace": "bench",
        "spec": spec,
        "provider": provider,
        "permissions": {"mode": "skip_dangerous", "project_root": "/"},
    }
    if job.get("stores") == "postgres":
        out["stores"] = {"type": "postgres", "dsn": job["pg_dsn"]}
    out.update(extra)
    return out


async def run_once(agent, session_id: str, prompt: str = "go") -> list:
    run = await agent.run(session_id, prompt)
    envelopes = [envelope async for envelope in run]
    result = await run.result()
    if not isinstance(result, ResultEvent) or result.subtype != "success":
        raise RuntimeError(f"run ended {result!r}")
    return envelopes


def rss(server: Server) -> dict:
    client = peak_rss_mb()
    sidecar = proc_hwm_mb(server.pid) or 0.0
    return {"peak_rss_mb": client + sidecar, "client_rss_mb": client, "sidecar_rss_mb": sidecar}


async def case_loop(job: Job) -> Result:
    turns = load_turns(job["fixture"])
    iters, warmup = job["iterations"], job["warmup"]
    scripted = {"id": "scripted", "turns": turns * (warmup + iters)}
    async with Server(binary=job["server_bin"]) as server:
        agent = await server.open_agent(config(job, scripted), tools=[echo_tool(job["tool"])])
        clock = Clock(lambda: [server.pid])
        for i in range(warmup):
            await run_once(agent, f"w{i}")
        for i in range(iters):
            clock.start()
            await run_once(agent, f"s{i}")
            clock.stop()
        memory = rss(server)
    peak = memory.pop("peak_rss_mb")
    return ok(clock, len(turns), "turn", peak, runs_total=warmup + iters, **memory)


CASES = {"loop": case_loop, "pg_loop": case_loop}

if __name__ == "__main__":
    main(CASES, asyncio.run)
