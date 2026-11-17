"""Feature 5: scheduler and worker: exactly-once fires across kill -9, coalescing after downtime,
run_bound failures visible in job.list, retry and give-up logged on stderr.

Usage: python scenario_sched.py s1|s2|s3|s4 ...
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

import h

from linch_rust import Server
from linch_rust.scripted import MODEL, text_turn

TOKEN = "tok-du-sched-0001"
HERE = Path(__file__).resolve().parent


def worker_conf(
    dsn: str,
    allow: dict[str, Any],
    *,
    directory: Path,
    worker: dict[str, Any] | None = None,
    **top: Any,
) -> dict[str, Any]:
    return {
        "socket": str(directory / "s.sock"),
        "tenants": {"acme": {"tokens": [{"value": TOKEN}], "allow": allow}},
        "worker": {
            "dsn": {"value": dsn},
            "poll_interval_ms": 250,
            "retry_delay_ms": 200,
            **(worker or {}),
        },
        **top,
    }


def scripted_cfg(dsn: str, ns: str = "acme", ttl: int = 2000) -> dict[str, Any]:
    return {
        "namespace": ns,
        "spec": {"model": MODEL, "max_turns": 2},
        "provider": {"id": "scripted", "turns": [text_turn("ok")] * 30},
        "stores": {"type": "postgres", "dsn": dsn, "lease_ttl_ms": ttl},
    }


async def s1(proxy: h.CountingProxy) -> dict[str, Any]:
    """5.1 each fire runs exactly once across kill -9 of the server (vLLM, 4 s schedule)."""
    out: dict[str, Any] = {"load": h.load()}
    with h.schema() as dsn:
        proxy.reset()
        d = Path(tempfile.mkdtemp(prefix="du-s1-"))
        allow = h.tenant(
            dsn,
            urls=[proxy.url],
            max_schedules=4,
            max_queued_jobs=16,
            min_schedule_interval_ms=1000,
        )
        conf = worker_conf(dsn, allow, directory=d, worker={"visibility_timeout_ms": 3000})
        config = h.cfg(dsn, proxy.url, "acme", ttl=2000)
        srv = h.Srv(conf, directory=d)
        await srv.start()
        kills = 0
        logs = []
        async with srv.connect(TOKEN) as server:
            agent = await server.open_agent(config)
            await agent.create_schedule("Reply with exactly: PONG", interval_ms=4000, id="beat")
        t_end = time.time() + 75
        while time.time() < t_end:
            running = h.q(dsn, "SELECT count(*) FROM linch_worker_jobs WHERE owner IS NOT NULL")[0][
                0
            ]
            if running and kills < 3:
                kills += 1
                await srv.kill()
                logs.append(srv.log)
                h.log("killed a server with a running job; kills", kills)
                await asyncio.sleep(1.0)
                srv = h.Srv(conf, directory=d)
                await srv.start()
                await asyncio.sleep(4)
            await asyncio.sleep(0.3)
        # Stop the schedule, let everything finish.
        async with srv.connect(TOKEN) as server:
            agent = await server.open_agent(config)
            await agent.pause_schedule("beat")
            await h.until(
                lambda: h.q(dsn, "SELECT count(*) FROM linch_worker_jobs")[0][0] == 0,
                timeout=120,
                what="the queue to drain",
            )
            sched = (await agent.schedules())[0]
            jobs = await agent.jobs(limit=200)
            out["kills"] = kills
            out["schedule"] = {k: sched.get(k) for k in ("fires", "skips", "paused", "last_fire")}
            hist = h.q(
                dsn,
                "SELECT schedule_id, fire_at, run_id, outcome, attempts FROM linch_worker_history "
                "ORDER BY fire_at",
            )
            out["history_rows"] = len(hist)
            out["distinct_fire_at"] = len({r[1] for r in hist})
            out["distinct_runs"] = len({r[2] for r in hist})
            out["outcomes"] = dict(Counter(r[3] for r in hist))
            out["attempts_gt1"] = sum(1 for r in hist if r[4] and r[4] > 1)
            out["runs_in_linch_runs"] = h.q(dsn, "SELECT count(*) FROM linch_runs")[0][0]
            out["run_status"] = dict(
                Counter(r[0] for r in h.q(dsn, "SELECT status FROM linch_runs"))
            )
            dup = 0
            per_run = {}
            for r in hist:
                if not r[2]:
                    continue
                ev = await agent.events(r[2])
                a = sum(1 for e in ev if e.event.type == "assistant")
                per_run[r[2][:8]] = a
                dup += a > 1
            out["runs_with_more_than_one_assistant"] = dup
            out["assistants_per_run"] = dict(Counter(per_run.values()))
            out["jobs_failed"] = sum(1 for j in jobs if j["state"] == "failed")
        out["proxy_calls"] = proxy.n()
        out["proxy_calls_states"] = dict(Counter(c.get("state") for c in proxy.calls))
        out["fires_vs_history"] = (out["schedule"]["fires"], out["history_rows"])
        await srv.kill()
        logs.append(srv.log)
        out["stderr_warn_lines"] = [
            ln
            for s in logs
            for ln in s.splitlines()
            if "warn" in ln.lower() or "fail" in ln.lower()
        ][:10]
    return out


async def s2() -> dict[str, Any]:
    """5.2 a 60 s heartbeat; the server is down for 190 s; one fire after the restart, the rest in "
    "skips."""
    out: dict[str, Any] = {"load": h.load()}
    with h.schema() as dsn:
        d = Path(tempfile.mkdtemp(prefix="du-s2-"))
        allow = h.tenant(dsn, urls=["http://127.0.0.1:1/v1"], max_schedules=4, max_queued_jobs=16)
        conf = worker_conf(dsn, allow, directory=d)
        srv = h.Srv(conf, directory=d)
        await srv.start()
        cfg = scripted_cfg(dsn)
        async with srv.connect(TOKEN) as server:
            agent = await server.open_agent(cfg)
            hb = await agent.heartbeat("hb-room", interval_ms=60000, id="hb")
            out["created"] = {k: hb.get(k) for k in ("next_fire", "interval_ms")}

            async def fired() -> bool:
                return (await agent.schedules())[0].get("fires", 0) >= 1

            await h.until(fired, timeout=90, every=1, what="the first fire")
            before = (await agent.schedules())[0]
        out["before_stop"] = {
            k: before.get(k) for k in ("fires", "skips", "last_fire", "next_fire")
        }
        await srv.stop()
        t_stop = time.time()
        h.log("s2 server stopped at", time.strftime("%H:%M:%S"), "waiting 190 s")
        await asyncio.sleep(190)
        runs_before = h.q(dsn, "SELECT count(*) FROM linch_runs")[0][0]
        srv = h.Srv(conf, directory=d)
        await srv.start()
        t_up = time.time()
        out["down_s"] = round(t_up - t_stop, 1)
        async with srv.connect(TOKEN) as server:
            agent = await server.open_agent(cfg)
            await asyncio.sleep(6)
            after = (await agent.schedules())[0]
            out["after_restart_6s"] = {
                k: after.get(k) for k in ("fires", "skips", "last_fire", "next_fire")
            }
            jobs = await agent.jobs(limit=50)
            out["jobs_total"] = len(jobs)
            out["runs_started_after_restart"] = (
                h.q(dsn, "SELECT count(*) FROM linch_runs")[0][0] - runs_before
            )
            await asyncio.sleep(5)
            out["after_restart_11s"] = {
                k: v
                for k, v in (await agent.schedules())[0].items()
                if k in ("fires", "skips", "next_fire")
            }
        await srv.kill()
        out["stderr"] = srv.log[-300:]
    return out


async def s3(proxy: h.CountingProxy) -> dict[str, Any]:
    """5.3 a stored tenant definition with no max_turns and no budget fails its fire, text in "
    "job.list."""
    out: dict[str, Any] = {"load": h.load()}
    with h.schema() as dsn:
        proxy.reset()
        d = Path(tempfile.mkdtemp(prefix="du-s3-"))
        allow = h.tenant(
            dsn,
            urls=[proxy.url],
            max_schedules=4,
            max_queued_jobs=16,
            min_schedule_interval_ms=1000,
        )
        conf = worker_conf(dsn, allow, directory=d)
        unbounded = h.cfg(dsn, proxy.url, "acme/unb", ttl=2000)
        del unbounded["spec"]["max_turns"]
        bounded = h.cfg(dsn, proxy.url, "acme/unb", ttl=2000)
        # A stdio client has no tenant: it stores the unbounded definition (as an old release
        # could).
        async with Server() as stdio:
            ua = await stdio.open_agent(unbounded)  # pyright: ignore[reportArgumentType]
            await ua.create_schedule("Reply with exactly: PONG", interval_ms=2000, id="unb")
            await ua.enqueue("unb-job", "Reply with exactly: PONG")
        srv = h.Srv(conf, directory=d)
        await srv.start()
        async with srv.connect(TOKEN) as server:
            # a bounded agent of the same namespace lists the jobs, and runs another job
            try:
                await server.open_agent(unbounded)
                out["tenant_open_unbounded"] = "accepted"
            except Exception as error:
                out["tenant_open_unbounded"] = f"{type(error).__name__}: {error}"
            agent = await server.open_agent(bounded)
            ok_job = await agent.enqueue("ok-job", "Reply with exactly: PONG")

            async def settled() -> bool:
                jobs = await agent.jobs(limit=50)
                return any(j["state"] == "failed" for j in jobs) and any(
                    j["job_id"] == ok_job["job_id"] and j["state"] == "done" for j in jobs
                )

            await h.until(settled, timeout=60, every=1, what="the failed job and the good one")
            await asyncio.sleep(5)
            jobs = await agent.jobs(limit=50)
            out["job_states"] = dict(Counter(j["state"] for j in jobs))
            failed = [j for j in jobs if j["state"] == "failed"]
            out["failed_errors"] = sorted({j.get("error", "") for j in failed})
            out["failed_fires"] = len([j for j in failed if j.get("schedule_id") == "unb"])
            out["good_job"] = next(j["state"] for j in jobs if j["job_id"] == ok_job["job_id"])
            sch = await agent.schedules()
            out["schedules"] = [
                {k: s.get(k) for k in ("id", "paused", "fires", "skips")} for s in sch
            ]
        out["proxy_calls"] = proxy.n()
        out["stderr"] = srv.log[-800:]
        await srv.kill()
    return out


async def s4() -> dict[str, Any]:
    """5.4 a job that cannot open its agent is retried and given up; both on stderr "
    "(LINCH_LOG=warn)."""
    out: dict[str, Any] = {"load": h.load()}
    with h.schema() as dsn:
        d = Path(tempfile.mkdtemp(prefix="du-s4-"))
        mcp = d / "mcp_tiny_server.py"
        shutil.copy(HERE.parent / "mcp_tiny_server.py", mcp)
        allow = h.tenant(dsn, urls=["http://127.0.0.1:1/v1"], max_queued_jobs=16)
        conf = worker_conf(dsn, allow, directory=d, worker={"local_namespaces": ["dev"]})
        cfg = scripted_cfg(dsn, ns="dev/retry")
        cfg["mcp_servers"] = [
            {
                "name": "tiny",
                "transport": {"type": "stdio", "command": sys.executable, "args": [str(mcp)]},
            }
        ]
        cfg["permissions"] = {"mode": "skip_dangerous", "project_root": "/"}
        async with Server() as stdio:
            agent = await stdio.open_agent(cfg)  # pyright: ignore[reportArgumentType]
            job = await agent.enqueue("retry-job", "hello")
            mcp.unlink()  # the worker cannot start the agent's MCP server any more
            srv = h.Srv(conf, directory=d, env={"LINCH_LOG": "warn"})
            t0 = time.time()
            await srv.start()
            states = []

            async def gone() -> bool:
                j = await agent.job(job["job_id"])
                states.append(
                    (round(time.time() - t0, 1), j["state"], j.get("failures"), j.get("attempts"))
                )
                return j["state"] == "failed"

            await h.until(gone, timeout=90, every=0.5, what="the job to give up")
            final = await agent.job(job["job_id"])
            out["final"] = {k: final.get(k) for k in ("state", "attempts", "failures", "error")}
            out["state_trace"] = [
                s for i, s in enumerate(states) if i == 0 or s[1:] != states[i - 1][1:]
            ]
            out["seconds_to_give_up"] = round(time.time() - t0, 1)
            await srv.kill()
            lines = srv.log.splitlines()
            out["stderr_lines"] = [ln[:260] for ln in lines]
            out["retrying_lines"] = sum("retrying a job" in ln for ln in lines)
            out["giving_up_lines"] = sum("gave up after" in ln for ln in lines)
        # Default level: the same server with no LINCH_LOG: warnings are on by default
    return out


async def main() -> None:
    proxy = h.CountingProxy()
    for name in sys.argv[1:] or ["s1", "s3", "s4"]:
        h.log("start", name)
        try:
            r = await {"s1": lambda: s1(proxy), "s2": s2, "s3": lambda: s3(proxy), "s4": s4}[name]()
        except Exception as error:
            import traceback

            r = {"error": repr(error), "tb": traceback.format_exc()}
        h.dump(f"sched_{name}.json", r)
        h.log(name, r)
    proxy.close()


asyncio.run(main())
