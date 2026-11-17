"""Feature 7 and the measurements: Postgres commits per turn, resume latency, throughput,
write overhead per turn. Scripted provider (no model call), stdio server, release binary.

Usage: python scenario_perf.py commits|resume|throughput|overhead ...
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import statistics
import sys
import time
from typing import Any

import h
import psycopg

from linch_rust import Server, callback, tool
from linch_rust.scripted import MODEL, text_turn, tool_turn


def multi_tool_turn(ids: list[str]) -> dict[str, Any]:
    events: list[dict[str, Any]] = [{"type": "message_start", "model": MODEL}]
    for i in ids:
        t = tool_turn(i, "lookup", {"topic": i})[
            "events"
        ]  # distinct inputs: the loop guard ends a repeat
        events += t[1:-1]
    events.append(tool_turn("z", "lookup", {})["events"][-1])
    return {"events": events}


def make_tool() -> Any:
    @tool(scope="read")
    async def lookup(topic: str = "x") -> str:
        """Look up a topic."""
        return "found"

    return lookup


def scripted_config(
    dsn: str | None, turns: list[dict[str, Any]], ns: str = "perf", ttl: int = 60000
) -> dict[str, Any]:
    c: dict[str, Any] = {
        "namespace": ns,
        "spec": {"model": MODEL, "max_turns": 20},
        "provider": {"id": "scripted", "turns": turns},
    }
    if dsn:
        c["stores"] = {"type": "postgres", "dsn": dsn, "lease_ttl_ms": ttl}
    return c


def stat() -> tuple[int, int]:
    with psycopg.connect(h.PG_DSN, autocommit=True) as c:
        row = c.execute(
            "SELECT xact_commit, xact_rollback FROM pg_stat_database WHERE datname = current_database()"
        ).fetchone()
        assert row is not None
        return int(row[0]), int(row[1])


def table_stats(schema_name: str) -> dict[str, tuple[int, int]]:
    with psycopg.connect(h.PG_DSN, autocommit=True) as c:
        rows = c.execute(
            "SELECT relname, n_tup_ins, n_tup_upd FROM pg_stat_user_tables WHERE schemaname = %s",
            (schema_name,),
        ).fetchall()
    return {r[0]: (int(r[1]), int(r[2])) for r in rows}


def schema_of(dsn: str) -> str:
    return dsn.split("search_path%3D")[1]


async def settle() -> None:
    await asyncio.sleep(2.5)  # stats of an idle backend flush within about a second


async def commits() -> dict[str, Any]:
    out: dict[str, Any] = {
        "load": h.load(),
        "binary": os.environ["LINCH_SERVER_BIN"],
        "ttl_ms": 60000,
    }
    # control: reading twice costs one commit; an idle open agent costs its renewals only
    a = stat()
    b = stat()
    out["read_twice_delta"] = b[0] - a[0]
    rows = []
    plan = [(0, 1), (1, 1), (2, 1), (3, 1), (1, 2), (2, 2)]  # (tool turns, tools per turn)
    for rep in range(3):
        for n, k in plan:
            turns = [
                (
                    tool_turn(f"c{i}", "lookup", {"topic": "x"})
                    if k == 1
                    else multi_tool_turn([f"c{i}-{j}" for j in range(k)])
                )
                for i in range(n)
            ] + [text_turn("done")]
            with h.schema() as dsn:
                async with Server() as server:
                    agent = await server.open_agent(
                        scripted_config(dsn, turns), tools=[make_tool()]
                    )
                    await settle()
                    s0 = stat()
                    table_stats(schema_of(dsn))
                    idle0 = time.time()
                    await asyncio.sleep(4.0)  # idle control, same agent open
                    await settle()
                    s_idle = stat()
                    s1 = stat()
                    t1 = table_stats(schema_of(dsn))
                    run = await agent.run("s1", "go")
                    result = await run.result()
                    await settle()
                    s2 = stat()
                    t2 = table_stats(schema_of(dsn))
                    tables = {
                        k2: (t2[k2][0] - t1.get(k2, (0, 0))[0], t2[k2][1] - t1.get(k2, (0, 0))[1])
                        for k2 in t2
                        if t2[k2] != t1.get(k2, (0, 0))
                    }
                    rows.append(
                        {
                            "rep": rep,
                            "tool_turns": n,
                            "tools_per_turn": k,
                            "result": getattr(result, "subtype", None),
                            "idle_commits": s_idle[0] - s0[0] - 2,  # minus the two control reads
                            "run_commits_raw": s2[0] - s1[0],
                            "run_commits": s2[0]
                            - s1[0]
                            - 2,  # minus the two control reads (stat, table_stats)
                            "rollbacks": s2[1] - s1[1],
                            "table_ins_upd": tables,
                        }
                    )
                    h.log(
                        rows[-1]["tool_turns"], k, rows[-1]["run_commits"], rows[-1]["idle_commits"]
                    )
    out["rows"] = rows

    # marginal commits per added tool turn (k=1): regress mean commits on tool_turns
    def mean(n: int, k: int) -> float:
        return statistics.mean(
            r["run_commits"] for r in rows if r["tool_turns"] == n and r["tools_per_turn"] == k
        )

    out["mean_commits"] = {f"{n}x{k}": mean(n, k) for n, k in plan}
    out["marginal_per_tool_turn_k1"] = [mean(n + 1, 1) - mean(n, 1) for n in range(3)]
    out["marginal_per_tool_turn_k2"] = [mean(2, 2) - mean(1, 2)]
    return out


async def resume_latency(reps: int) -> dict[str, Any]:
    out: dict[str, Any] = {
        "load": h.load(),
        "binary": os.environ["LINCH_SERVER_BIN"],
        "ttl_ms": 1000,
    }
    samples = []
    for rep in range(reps):
        with h.schema() as dsn:
            config = scripted_config(dsn, [text_turn("ok")], ttl=1000)
            holder: dict[str, Any] = {"pid": None, "armed": True}

            def make_cb(kill: bool) -> Any:
                @callback("before_provider_call", "bpc", 1)
                async def bpc(req: dict[str, Any]) -> dict[str, Any]:
                    if kill and holder["armed"]:
                        holder["armed"] = False
                        os.kill(holder["pid"], signal.SIGKILL)
                        await asyncio.sleep(30)
                    return {"action": "continue"}

                return bpc

            async with Server() as s1:
                holder["pid"] = s1.pid
                agent = await s1.open_agent(config, callbacks=[make_cb(True)])
                run = await agent.run("s1", "go")
                try:
                    await asyncio.wait_for(run.result(), 30)
                except Exception:
                    pass
            await asyncio.sleep(1.6)  # the lease (1 s) is over: the resume does not wait for it
            t0 = time.time()
            async with Server() as s2:
                t_spawn = time.time()
                agent2 = await s2.open_agent(config, callbacks=[make_cb(False)])
                t_open = time.time()
                r2 = await agent2.resume(run.id)
                t_call = time.time()
                first = None
                async for env in r2:
                    first = time.time()
                    break
                result = await r2.result()
                t_done = time.time()
            samples.append(
                {
                    "rep": rep,
                    "spawn_to_open_ms": round((t_open - t0) * 1000, 1),
                    "open_to_first_event_ms": round((first - t_open) * 1000, 1) if first else None,
                    "start_to_first_event_ms": round((first - t0) * 1000, 1) if first else None,
                    "start_to_result_ms": round((t_done - t0) * 1000, 1),
                    "result": getattr(result, "subtype", None),
                }
            )
            h.log(samples[-1])
    out["samples"] = samples
    for key in ("start_to_first_event_ms", "open_to_first_event_ms", "start_to_result_ms"):
        vals = sorted(s[key] for s in samples if s[key] is not None)
        out[f"p50_{key}"] = statistics.median(vals)
        out[f"min_max_{key}"] = (vals[0], vals[-1])
    return out


async def one_batch(agent: Any, count: int, conc: int, prefix: str) -> float:
    sem = asyncio.Semaphore(conc)

    async def go(i: int) -> Any:
        async with sem:
            run = await agent.run(f"{prefix}-{i}", "go")
            return await run.result()

    t0 = time.time()
    results = await asyncio.gather(*(go(i) for i in range(count)))
    dt = time.time() - t0
    assert all(getattr(r, "subtype", None) == "success" for r in results), results[:2]
    return dt


async def throughput(reps: int) -> dict[str, Any]:
    out: dict[str, Any] = {
        "load": h.load(),
        "binary": os.environ["LINCH_SERVER_BIN"],
        "runs": 20,
        "cores": os.cpu_count(),
    }
    rows = []
    for store in ("postgres", "memory"):
        for conc in (1, 4, 16):
            vals = []
            for rep in range(reps):
                with h.schema() as dsn:
                    # the provider's script advances with every call of the agent: one turn per run
                    cfg = scripted_config(
                        dsn if store == "postgres" else None, [text_turn("ok")] * 30
                    )
                    async with Server() as server:
                        agent = await server.open_agent(cfg)
                        await one_batch(agent, 2, 1, "warm")
                        dt = await one_batch(agent, 20, conc, f"b{rep}")
                        vals.append(dt)
                h.log(store, conc, rep, round(20 / dt, 1), "runs/s")
            rows.append(
                {
                    "store": store,
                    "concurrency": conc,
                    "load_after": h.load(),
                    "seconds_per_20": [round(v, 3) for v in vals],
                    "runs_per_s": [round(20 / v, 1) for v in vals],
                    "runs_per_s_median": round(statistics.median(20 / v for v in vals), 1),
                }
            )
    out["rows"] = rows
    return out


async def overhead(reps: int) -> dict[str, Any]:
    """The same 3-tool-turn run (4 provider calls, 3 tool calls) on memory and on Postgres, one at a time."""
    out: dict[str, Any] = {"load": h.load(), "binary": os.environ["LINCH_SERVER_BIN"]}
    turns = [
        tool_turn(f"c{r}-{i}", "lookup", {"topic": f"t{r}-{i}"}) if i < 3 else text_turn("done")
        for r in range(12)  # the script advances with every call: one cycle per run (1 warm + 10)
        for i in range(4)
    ]
    res: dict[str, list[float]] = {"memory": [], "postgres": []}
    for store in ("memory", "postgres"):
        for rep in range(reps):
            with h.schema() as dsn:
                cfg = scripted_config(dsn if store == "postgres" else None, turns)
                async with Server() as server:
                    agent = await server.open_agent(cfg, tools=[make_tool()])
                    await (await agent.run("warm", "go")).result()
                    for i in range(10):
                        t0 = time.time()
                        r = await (await agent.run(f"s{i}", "go")).result()
                        res[store].append((time.time() - t0) * 1000)
                        assert getattr(r, "subtype", None) == "success"
    out["per_run_ms"] = {
        k: {
            "median": round(statistics.median(v), 2),
            "mean": round(statistics.mean(v), 2),
            "min": round(min(v), 2),
            "n": len(v),
        }
        for k, v in res.items()
    }
    out["provider_calls_per_run"] = 4
    out["tool_calls_per_run"] = 3
    d = out["per_run_ms"]["postgres"]["median"] - out["per_run_ms"]["memory"]["median"]
    out["postgres_minus_memory_ms_per_run"] = round(d, 2)
    out["postgres_minus_memory_ms_per_turn"] = round(d / 4, 2)
    return out


async def main() -> None:
    which = sys.argv[1]
    reps = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    if which == "commits":
        r = await commits()
    elif which == "resume":
        r = await resume_latency(reps)
    elif which == "throughput":
        r = await throughput(reps)
    else:
        r = await overhead(reps)
    name = f"perf_{which}"
    if os.environ.get("TAG"):
        name += os.environ["TAG"]
    h.dump(name + ".json", r)
    print(json.dumps({k: v for k, v in r.items() if k not in ("rows", "samples")}, default=str))


if __name__ == "__main__":
    asyncio.run(main())
