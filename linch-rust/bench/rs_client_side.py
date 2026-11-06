"""Bench side "rs-client": the `linch_rust` client over the `linch-server` sidecar.

Run as `python -I rs_client_side.py JOB.json`; imports the client from
`linch-rust/python/src` and spawns the server binary the job names. CPU
counts this process plus the sidecar's threads; peak RSS is the sum of the
two processes' peaks.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "python" / "src"))

from common import (  # noqa: E402
    Clock,
    Job,
    Result,
    load_turns,
    main,
    na,
    ok,
    peak_rss_mb,
    proc_hwm_mb,
)


def lr():
    """The client package, imported on first use so `startup` can time the import."""
    import linch_rust

    return linch_rust


def echo_tool(spec: dict, reply: str | None = None):
    """Echo as a host tool; with `reply` (the live `get_weather`) it answers
    that template with `{city}` filled in."""

    @lr().tool(
        scope="read",
        name=spec["name"],
        description=spec["description"],
        input_schema=spec["input_schema"],
    )
    async def echo(**input: str) -> str:
        if reply is not None:
            return reply.replace("{city}", input.get("city", "there"))
        return input["value"]

    return echo


def config(job: Job, provider: dict, **extra) -> dict:
    spec = {"model": job["model"], "system_prompt": job["system"]}
    if job.get("max_tool_concurrency") is not None:
        spec["max_tool_concurrency"] = job["max_tool_concurrency"]
    spec.update(job.get("spec", {}))
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
    for envelope in envelopes:
        event = envelope.event
        if event.type == "tool_call_end" and event.result.get("is_error"):
            raise RuntimeError(f"{event.tool_name} failed: {str(event.result)[:300]}")
    result = await run.result()
    if not isinstance(result, lr().ResultEvent) or result.subtype != "success":
        raise RuntimeError(f"run ended {result!r}")
    return envelopes


def rss(server) -> dict:
    client = peak_rss_mb()
    sidecar = proc_hwm_mb(server.pid) or 0.0
    return {"peak_rss_mb": client + sidecar, "client_rss_mb": client, "sidecar_rss_mb": sidecar}


async def timed_runs(job: Job, agent, server, before=None) -> Clock:
    clock = Clock(lambda: [server.pid])
    for i in range(job["warmup"]):
        if before is not None:
            before()
        await run_once(agent, f"w{i}")
    for i in range(job["iterations"]):
        if before is not None:
            before()
        clock.start()
        clock.last = await run_once(agent, f"s{i}")
        clock.stop()
    return clock


def scripted(job: Job, turns: list[dict]) -> dict:
    return {"id": "scripted", "turns": turns * (job["warmup"] + job["iterations"])}


async def case_loop(job: Job) -> Result:
    turns = load_turns(job["fixture"])
    async with lr().Server(binary=job["server_bin"]) as server:
        cfg = config(job, scripted(job, turns))
        agent = await server.open_agent(cfg, tools=[echo_tool(job["tool"])])
        clock = await timed_runs(job, agent, server)
        memory = rss(server)
    peak = memory.pop("peak_rss_mb")
    runs = job["warmup"] + job["iterations"]
    return ok(clock, len(turns), "turn", peak, runs_total=runs, **memory)


def reset_workspace(job: Job) -> None:
    src = Path(job["workspace"]) / "src"
    shutil.rmtree(src)
    shutil.copytree(job["pristine"], src)


async def case_tools(job: Job) -> Result:
    turns = load_turns(job["fixture"])
    async with lr().Server(binary=job["server_bin"]) as server:
        cfg = config(job, scripted(job, turns), workspace=job["workspace"])
        agent = await server.open_agent(cfg)
        clock = await timed_runs(job, agent, server, lambda: reset_workspace(job))
        memory = rss(server)
    return ok(clock, job["calls"], "call", memory.pop("peak_rss_mb"), **memory)


async def case_mcp(job: Job) -> Result:
    turns = load_turns(job["fixture"])
    transport = {"type": "stdio", "command": job["mcp_server_bin"], "args": []}
    servers = [{"name": "bench", "transport": transport}]
    async with lr().Server(binary=job["server_bin"]) as server:
        agent = await server.open_agent(config(job, scripted(job, turns), mcp_servers=servers))
        clock = await timed_runs(job, agent, server)
        memory = rss(server)
    return ok(clock, job["calls"], "call", memory.pop("peak_rss_mb"), **memory)


async def case_compaction(job: Job) -> Result:
    turns = load_turns(job["fixture"])
    cfg = config(job, scripted(job, turns))
    cfg["spec"]["compaction"] = {
        "threshold_tokens": job["rs_threshold_tokens"],
        "prompt": job["summary_prompt"],
    }
    compactions = []
    async with lr().Server(binary=job["server_bin"]) as server:
        agent = await server.open_agent(cfg, tools=[echo_tool(job["tool"])])
        clock = Clock(lambda: [server.pid])
        for i in range(job["warmup"] + job["iterations"]):
            timed = i >= job["warmup"]
            if timed:
                clock.start()
            envelopes = await run_once(agent, f"s{i}")
            if timed:
                clock.stop()
            compactions.append(sum(e.event.type == "compaction" for e in envelopes))
        memory = rss(server)
    if min(compactions) < 1:
        raise RuntimeError(f"a run did not compact: {compactions}")
    note = f"{compactions[-1]} compaction(s)/run"
    peak = memory.pop("peak_rss_mb")
    return ok(clock, len(turns) - 1, "turn", peak, note=note, **memory)


async def case_concurrency(job: Job) -> Result:
    turns = load_turns(job["fixture"])
    async with lr().Server(binary=job["server_bin"]) as server:
        agents = [
            await server.open_agent(
                config(job, scripted(job, turns)), tools=[echo_tool(job["tool"])]
            )
            for _ in range(job["sessions"])
        ]
        clock = Clock(lambda: [server.pid])
        for i in range(job["warmup"] + job["iterations"]):
            timed = i >= job["warmup"]
            if timed:
                clock.start()
            await asyncio.gather(*(run_once(agent, f"s{i}") for agent in agents))
            if timed:
                clock.stop()
        memory = rss(server)
    return ok(clock, job["sessions"], "run", memory.pop("peak_rss_mb"), **memory)


async def case_startup(job: Job) -> Result:
    """One cold process: import, server start and open, first run."""
    started = time.perf_counter()
    client = lr()
    imported = time.perf_counter()
    async with client.Server(binary=job["server_bin"]) as server:
        agent = await server.open_agent(
            config(job, {"id": "scripted", "turns": load_turns(job["fixture"])}),
            tools=[echo_tool(job["tool"])],
        )
        opened = time.perf_counter()
        await run_once(agent, "s0")
        done = time.perf_counter()
        memory = rss(server)
    return {
        "status": "ok",
        "phases_ms": {
            "import": 1000 * (imported - started),
            "open": 1000 * (opened - imported),
            "first_run": 1000 * (done - opened),
        },
        **memory,
    }


async def case_decode(job: Job) -> Result:
    """A one-turn run whose stream is the vendor's replayed SSE stream."""
    provider = {"id": job["vendor"], "base_url": job["base_url"]}
    if job["vendor"] == "openai-responses":
        provider["store"] = False
    async with lr().Server(binary=job["server_bin"]) as server:
        agent = await server.open_agent(config(job, provider))
        clock = await timed_runs(job, agent, server)
        memory = rss(server)
    text = next(e.event.final_text for e in clock.last if e.event.type == "result") or ""
    if len(text) != job["expect_chars"]:
        raise RuntimeError(f"decoded {len(text)} characters, expected {job['expect_chars']}")
    peak = memory.pop("peak_rss_mb")
    extra = {"bytes_per_iter": job["stream_bytes"], **memory}
    return ok(clock, job["stream_events"], "event", peak, **extra)


async def case_live(job: Job) -> Result:
    """The W5 live prompts on DeepSeek; the sidecar reads the key from its
    environment, which is this process's."""
    tool = echo_tool(job["tool"], reply=job["tool_reply"])
    turns = 0
    async with lr().Server(binary=job["server_bin"]) as server:
        agent = await server.open_agent(config(job, {"id": "deepseek"}), tools=[tool])
        clock = Clock(lambda: [server.pid])
        for i, prompt in enumerate(job["prompts"]):
            clock.start()
            envelopes = await run_once(agent, f"live-{i}", prompt)
            clock.stop()
            turns += sum(e.event.type == "assistant" for e in envelopes)
        memory = rss(server)
    per_run = turns / max(len(job["prompts"]), 1)
    return ok(clock, per_run, "turn", memory.pop("peak_rss_mb"), turns=turns, **memory)


async def not_in_client(job: Job) -> Result:
    what = {
        "worker": "the worker queue (linch-worker)",
        "workflow": "workflows (linch-ext)",
        "evals": "evals (linch-ext)",
    }[job["case"]]
    return na(f"{what} is Rust-only: neither linch-server nor the client exposes it")


CASES = {
    "decode": case_decode,
    "live": case_live,
    "worker": not_in_client,
    "workflow": not_in_client,
    "evals": not_in_client,
    "loop": case_loop,
    "pg_loop": case_loop,
    "tools": case_tools,
    "mcp": case_mcp,
    "compaction": case_compaction,
    "concurrency": case_concurrency,
    "startup": case_startup,
}

if __name__ == "__main__":
    main(CASES, asyncio.run)
