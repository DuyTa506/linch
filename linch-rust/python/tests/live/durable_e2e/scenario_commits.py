"""Feature 7: Postgres commits per turn.

Commits of a whole server lifetime (spawn, open agent, one run, exit), counted after the server's
backends exited: Postgres flushes a backend's pending counters when it exits, and a live backend
only within about 10 s of going idle (an earlier version of this script read 2.5 s after the run and
got a constant 29 whatever the run length: the counters had not flushed). The differences between
runs of different length are the cost per turn; the fixed cost (migrations check, claims, close)
cancels.
"""

from __future__ import annotations

import asyncio
import os
import statistics
from typing import Any

import h
from scenario_perf import make_tool, multi_tool_turn, schema_of, scripted_config, stat, table_stats

from linch_rust import Server
from linch_rust.scripted import text_turn, tool_turn


async def commits(reps: int) -> dict[str, Any]:
    out: dict[str, Any] = {
        "load": h.load(),
        "binary": os.environ["LINCH_SERVER_BIN"],
        "ttl_ms": 60000,
    }
    a = stat()
    b = stat()
    out["read_twice_delta"] = b[0] - a[0]
    rows = []
    # (tool turns, tools per turn); -1 = open the agent and close, no run
    plan = [(-1, 1), (0, 1), (1, 1), (2, 1), (3, 1), (1, 2), (2, 2)]
    for rep in range(reps):
        for n, k in plan:
            turns = [
                (
                    tool_turn(f"c{i}", "lookup", {"topic": f"t{i}"})
                    if k == 1
                    else multi_tool_turn([f"c{i}-{j}" for j in range(k)])
                )
                for i in range(max(n, 0))
            ] + [text_turn("done")]
            with h.schema() as dsn:
                sch = schema_of(dsn)
                s0 = stat()
                result = None
                async with Server() as server:
                    agent = await server.open_agent(
                        scripted_config(dsn, turns),
                        tools=[make_tool()],
                    )
                    if n >= 0:
                        run = await agent.run("s1", "go")
                        result = await run.result()
                    else:
                        await asyncio.sleep(1.0)
                await asyncio.sleep(1.5)
                s1 = stat()
                tables = table_stats(sch)
                rows.append(
                    {
                        "rep": rep,
                        "tool_turns": n,
                        "tools_per_turn": k,
                        "result": getattr(result, "subtype", None),
                        "commits": s1[0] - s0[0] - 1,  # minus the first read's own commit
                        "rollbacks": s1[1] - s0[1],
                        "tables_ins_upd": {t: v for t, v in tables.items() if v != (0, 0)},
                    }
                )
                h.log(n, k, rows[-1]["commits"])
    out["rows"] = rows

    def mean(n: int, k: int) -> float:
        return statistics.mean(
            r["commits"] for r in rows if r["tool_turns"] == n and r["tools_per_turn"] == k
        )

    out["mean_commits"] = {f"{n}x{k}": round(mean(n, k), 2) for n, k in plan}
    out["open_close_only"] = round(mean(-1, 1), 2)
    out["text_only_run_extra"] = round(mean(0, 1) - mean(-1, 1), 2)
    out["marginal_per_tool_turn_k1"] = [round(mean(n + 1, 1) - mean(n, 1), 2) for n in range(3)]
    out["marginal_per_tool_turn_k2"] = [round(mean(2, 2) - mean(1, 2), 2)]
    out["first_turn_k2_minus_k1"] = round(mean(1, 2) - mean(1, 1), 2)
    return out


async def main() -> None:
    import sys

    reps = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    r = await commits(reps)
    h.dump("perf_commits.json", r)
    print({k: v for k, v in r.items() if k != "rows"})


asyncio.run(main())
