"""Measurements of the toolsec area. Run these with the RELEASE binary (`BIN=release run.sh`).

M1 tool-call overhead (scripted provider, a no-op host tool and Bash `true`), M2 tool cache hit versus
miss of a 200 ms tool, M3 admission cost (tenant versus local), M4 server RSS over 100 runs,
M5 Bash spawn time (local and sandbox). Numbers go to `$TOOLSEC_RESULTS/bench.jsonl` with the load average.
"""

from __future__ import annotations

import asyncio
import os
import statistics
import time
from typing import Any

from ts_support import (
    ServerProc,
    needs_server,
    pctl,
    pytestmark,
    record,
    rss_kb,
    scripted_config,
    server_env,
    skip_dangerous,
    tenant,
    text_turn,
    tool_turn,
    workspace,
)

from linch_rust import Server, ToolCallEndEvent, tool

__all__ = ["pytestmark"]

AREA = "bench"


def summary(values: list[float]) -> dict[str, float]:
    return {
        "n": len(values),
        "p50": round(pctl(values, 0.5), 3),
        "p95": round(pctl(values, 0.95), 3),
        "mean": round(statistics.fmean(values), 3),
        "min": round(min(values), 3),
        "max": round(max(values), 3),
    }


async def timed_run(
    agent: Any, session: str, prompt: str = "go"
) -> tuple[list[float], list[int], float]:
    """Inter-arrival times (ms) of consecutive tool results, their reported durations, wall seconds."""
    stamps: list[float] = []
    durations: list[int] = []
    start = time.perf_counter()
    run = await agent.run(session, prompt)
    async for envelope in run:
        if isinstance(envelope.event, ToolCallEndEvent):
            stamps.append(time.perf_counter())
            durations.append(envelope.event.duration_ms)
    wall = time.perf_counter() - start
    gaps = [(b - a) * 1000 for a, b in zip(stamps, stamps[1:], strict=False)]
    return gaps, durations, wall


def tool_turns(name: str, make: Any, calls: int) -> list[dict[str, Any]]:
    turns = [tool_turn(f"c{i}", name, make(i)) for i in range(calls)]
    turns.append(text_turn("done"))
    return turns


@tool(scope="read")
async def noop(n: int) -> str:
    """Do nothing."""
    return "ok"


@needs_server
async def test_m1_m5_tool_call_overhead_and_bash_spawn() -> None:
    binary = os.environ["LINCH_SERVER_BIN"]
    out: dict[str, Any] = {"binary": os.path.basename(binary)}
    with workspace() as root:
        scenarios = {
            "host_noop": (lambda i: {"n": i}, "noop", {}),
            "bash_true_local": (lambda i: {"command": f"true # {i}"}, "Bash", {}),
            "bash_true_sandbox": (
                lambda i: {"command": f"true # {i}"},
                "Bash",
                {"execution": {"backend": "sandbox"}},
            ),
        }
        for label, (make, name, extra) in scenarios.items():
            runs = []
            async with Server(env=server_env()) as server:
                for repeat in range(3):
                    calls = 200 if label != "bash_true_sandbox" else 50
                    config = scripted_config(
                        tool_turns(name, make, calls),
                        spec={"max_turns": calls + 10},
                        workspace=str(root),
                        permissions=skip_dangerous(root),
                        **extra,
                    )
                    agent = await server.open_agent(config, tools=[noop] if name == "noop" else [])
                    gaps, durations, wall = await timed_run(agent, f"s{repeat}")
                    await agent.close()
                    runs.append(
                        {
                            "gap_ms": summary(gaps),
                            "reported_duration_ms": summary([float(d) for d in durations]),
                            "wall_s": round(wall, 3),
                            "per_call_wall_ms": round(wall * 1000 / calls, 3),
                        }
                    )
            all_gaps_p50 = statistics.median(r["gap_ms"]["p50"] for r in runs)
            all_gaps_p95 = statistics.median(r["gap_ms"]["p95"] for r in runs)
            out[label] = {
                "repeats": runs,
                "p50_ms_median_of_repeats": all_gaps_p50,
                "p95_ms_median_of_repeats": all_gaps_p95,
            }
    record(AREA, "M1-M5", **out)


@needs_server
async def test_m2_tool_cache_hit_versus_miss_of_a_200_ms_tool() -> None:
    delay = 0.2

    @tool(scope="read")
    async def slow(q: int) -> str:
        """A 200 ms lookup."""
        await asyncio.sleep(delay)
        return f"v{q}"

    pairs = 10
    turns = []
    for i in range(pairs):
        turns += [tool_turn(f"m{i}", "slow", {"q": i}), tool_turn(f"h{i}", "slow", {"q": i})]
    turns.append(text_turn("done"))
    misses: list[float] = []
    hits: list[float] = []
    hit_flags: list[bool] = []
    async with Server(env=server_env()) as server:
        for repeat in range(3):
            config = scripted_config(
                list(turns),
                spec={"max_turns": 60, "tool_cache": {"tools": ["slow"], "ttl_ms": 60000}},
            )
            agent = await server.open_agent(config, tools=[slow])
            stamps: list[float] = []
            flags: list[bool] = []
            run = await agent.run(f"s{repeat}", "go")
            t0 = time.perf_counter()
            async for envelope in run:
                if isinstance(envelope.event, ToolCallEndEvent):
                    stamps.append(time.perf_counter())
                    flags.append(envelope.event.cache_hit)
            gaps = [(b - a) * 1000 for a, b in zip([t0] + stamps, stamps, strict=False)]
            misses += gaps[0::2]
            hits += gaps[1::2]
            hit_flags += flags
            await agent.close()
    record(
        AREA,
        "M2",
        miss_ms=summary(misses),
        hit_ms=summary(hits),
        speedup_p50=round(pctl(misses, 0.5) / max(pctl(hits, 0.5), 0.001), 1),
        flags_alternate=hit_flags == [False, True] * (len(hit_flags) // 2),
    )
    assert hit_flags == [False, True] * (len(hit_flags) // 2)
    assert pctl(hits, 0.5) < 20 and pctl(misses, 0.5) >= 200


@needs_server
async def test_m3_admission_cost_tenant_versus_local() -> None:
    with workspace() as root:

        def config(name: str, namespace: str) -> Any:
            return scripted_config(
                [text_turn("x")],
                namespace=namespace,
                workspace=str(root),
                permissions={"mode": "accept_edits", "project_root": str(root)},
            )

        async def open_many(server: Server, prefix: str, tenant_ns: str | None) -> list[float]:
            times = []
            for i in range(20):
                namespace = f"{tenant_ns}/{prefix}{i}" if tenant_ns else f"{prefix}{i}"
                start = time.perf_counter()
                agent = await server.open_agent(config(prefix, namespace))
                times.append((time.perf_counter() - start) * 1000)
                await agent.close()
            return times

        local: list[float] = []
        remote: list[float] = []
        async with Server(env=server_env()) as server:
            await open_many(server, "warm", None)
            for r in range(3):
                local += await open_many(server, f"r{r}-", None)
        allow = {
            "scripted_provider": True,
            "workspace_roots": [str(root)],
            "max_agents": 32,
            "max_runs": 32,
        }
        proc = await ServerProc({"acme": tenant("tok-acme-bench-0001", **allow)}).start()
        try:
            async with proc.connect("tok-acme-bench-0001") as server:
                await open_many(server, "warm", "acme")
                for r in range(3):
                    remote += await open_many(server, f"r{r}-", "acme")
        finally:
            proc.cleanup()
    record(
        AREA,
        "M3",
        note="20 agents x 3 repeats each, each closed after its open; memory stores, scripted provider, workspace+accept_edits",
        local_stdio_ms=summary(local),
        tenant_socket_ms=summary(remote),
    )


@needs_server
async def test_m4_server_rss_over_100_runs() -> None:
    with workspace() as root:
        allow = {
            "scripted_provider": True,
            "workspace_roots": [str(root)],
            "max_agents": 8,
            "max_runs": 8,
            "exec_tools": True,
            "permission_modes": ["default", "skip_dangerous"],
        }
        proc = await ServerProc({"acme": tenant("tok-acme-bench-0001", **allow)}).start()
        try:
            samples = {"start": rss_kb(proc.pid)}
            async with proc.connect("tok-acme-bench-0001") as server:
                # one agent kept for 100 runs; each run is its own session with one Bash call
                turns = []
                for i in range(100):
                    turns += [
                        tool_turn(f"b{i}", "Bash", {"command": f"echo run-{i}"}),
                        text_turn(f"done {i}"),
                    ]
                config = scripted_config(
                    turns,
                    namespace="acme",
                    workspace=str(root),
                    permissions=skip_dangerous(root),
                )
                agent = await server.open_agent(config)
                samples["agent_open"] = rss_kb(proc.pid)
                for i in range(100):
                    run = await agent.run(f"s{i}", "go")
                    await run.result()
                    if (i + 1) % 25 == 0:
                        samples[f"after_{i + 1}_runs"] = rss_kb(proc.pid)
                await agent.close()
                await asyncio.sleep(0.5)
                samples["after_agent_close"] = rss_kb(proc.pid)
        finally:
            proc.cleanup()
    record(AREA, "M4", rss_kb=samples, note="release binary, memory stores, 1 Bash call per run")


@needs_server
async def test_m4b_rss_across_agent_cycles() -> None:
    """Whether memory freed by `agent.close` is reused: 4 rounds of (open, 100 runs, close)."""
    with workspace() as root:
        allow = {
            "scripted_provider": True,
            "workspace_roots": [str(root)],
            "max_agents": 8,
            "exec_tools": True,
            "permission_modes": ["default", "skip_dangerous"],
        }
        proc = await ServerProc({"acme": tenant("tok-acme-bench-0001", **allow)}).start()
        try:
            rounds = []
            async with proc.connect("tok-acme-bench-0001") as server:
                for round_no in range(int(os.environ.get("M4B_ROUNDS", "4"))):
                    turns = []
                    for i in range(100):
                        turns += [
                            tool_turn(f"b{i}", "Bash", {"command": f"echo r{i}"}),
                            text_turn("d"),
                        ]
                    config = scripted_config(
                        turns,
                        namespace="acme",
                        workspace=str(root),
                        permissions=skip_dangerous(root),
                    )
                    agent = await server.open_agent(config)
                    for i in range(100):
                        await (await agent.run(f"s{i}", "go")).result()
                    before_close = rss_kb(proc.pid)
                    await agent.close()
                    await asyncio.sleep(0.5)
                    rounds.append(
                        {
                            "round": round_no,
                            "before_close_kb": before_close,
                            "after_close_kb": rss_kb(proc.pid),
                        }
                    )
        finally:
            proc.cleanup()
    record(AREA, "M4b", rounds=rounds)
